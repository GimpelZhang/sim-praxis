"""D-8 thinking 接受性诊断探针的 hermetic 单测（0 付费、0 触网）。

覆盖：
- gateway 层：payload 正形（thinking 块 + 三件套同 _post_messages）、200/message
  仅 thinking 块也算接受（G-12 豁免：单发不重试）、thinking+text 块类型、
  4xx/5xx 明确 HTTP 响应不抛异常只记一行 ok=false、连接异常照记 ok=false 后抛、
  stage 白名单/参数校验在 preflight 之前拒绝、缺凭据 seq=null blocked；
- 红线：生产两传输路径（_chat_loop completion kwargs / _post_messages payload）
  AST 级不含 thinking 键；diag_thinking 生产调用点全仓仅探针脚本 1 处；
- 脚本层：裸跑付费守卫 exit 2 零触网、假网关离线落 0600 证据、exit 0/10 分支。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import types
from pathlib import Path

import pytest
import requests
import yaml

import orchestrator.gateway.router as R
from orchestrator.gateway.quota_ledger import (
    QuotaLedger,
    billing_context,
    write_approval,
)
from orchestrator.gateway.router import ModelGateway

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _REPO_ROOT / "scripts" / "phase2" / "diag_thinking.py"
ROUTER_PATH = _REPO_ROOT / "orchestrator" / "gateway" / "router.py"

BATCH_MACRO = "T6-B1-macro"
BATCH_CODING = "T6-B1-coding"
THREAD = "t6-genesis-ascent-20261016a"
DEFAULT_IDS = {
    "macro_ideation": "deepseek-v4.1-flash",
    "coding_debug": "glm-5.3-flash",
    "visual_rollout": "ark-code-latest",
}
TOKEN = "diag-thinking-test-key-redacted-0001"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", TOKEN)
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL",
                       "https://ark.example.invalid/api/coding")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    oq = tmp_path / "open-questions.md"
    oq.write_text("# Open Questions\n\n- 供应侧模型下线置 null 锚点\n",
                  encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_OPEN_QUESTIONS", str(oq))
    map_path = tmp_path / "stage_models.yaml"
    sm = {s: {"model_id": DEFAULT_IDS[s],
              "capabilities": ["text", "vision"] if s == R.VISION_STAGE else ["text"],
              "purpose": "t"} for s in R.STAGES}
    map_path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm,
    }, allow_unicode=True, width=4096), encoding="utf-8")
    yield {"tmp": tmp_path, "map": map_path}


def _gateway(env, raw_transport=None):
    return ModelGateway(stage_models_path=env["map"], sleep=lambda s: None,
                        raw_transport=raw_transport, ledger=QuotaLedger())


def _approve(stage, batch):
    write_approval(batch_id=batch, task=6, stage=stage, approved=3,
                   beyond_normal=False, note="diag fixture（零付费单测）")


def _rows(tmp):
    p = tmp / "phase2/quota/ledger.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _int_rows(tmp):
    return [r for r in _rows(tmp) if isinstance(r.get("seq"), int)]


def _resp(status, body):
    return types.SimpleNamespace(
        status_code=status, json=lambda: body,
        text=json.dumps(body, ensure_ascii=False), headers={})


def _message_body(*, blocks, model="deepseek-v4.1-flash",
                  in_tok=11, out_tok=4):
    return {"type": "message", "model": model, "content": blocks,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}


# ---------------------------------------------------------------------------
# 1. payload 正形：thinking 块 + 与 _post_messages 同形三件套/凭据/URL
# ---------------------------------------------------------------------------

def test_payload_well_formed_thinking_block(env):
    seen = {}

    def raw_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json)
        return _resp(200, _message_body(blocks=[{"type": "thinking",
                                                 "thinking": "…"},
                                                {"type": "text",
                                                 "text": "等于 2"}]))

    gw = _gateway(env, raw_post)
    _approve("macro_ideation", BATCH_MACRO)
    with billing_context(task=6, batch_id=BATCH_MACRO, thread_id=THREAD):
        rec = gw.diag_thinking("macro_ideation", max_tokens=2048,
                               budget_tokens=1024)
    p = seen["json"]
    # 与 _post_messages 同一 URL/headers 模式。
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert seen["headers"]["anthropic-version"] == R.ANTHROPIC_VERSION
    assert seen["headers"]["content-type"] == "application/json"
    # 三件套同形（model/max_tokens/messages: user+单 text block）。
    assert p["model"] == "deepseek-v4.1-flash"
    assert p["max_tokens"] == 2048
    msgs = p["messages"]
    assert msgs == [{"role": "user",
                     "content": [{"type": "text", "text": msgs[0]["content"][0]["text"]}]}]
    assert isinstance(msgs[0]["content"][0]["text"], str) and msgs[0]["content"][0]["text"]
    # 正形 thinking 块，budget 与请求一致；整包绝无 image block（硬规则1）。
    assert p["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "image" not in json.dumps(p)
    # 记录的请求形状只有键名 + 两个 token 参数；token/URL/请求文本不入记录。
    assert rec["request"] == {"keys": ["max_tokens", "messages", "model", "thinking"],
                              "max_tokens": 2048, "budget_tokens": 1024}
    assert TOKEN not in json.dumps(rec, ensure_ascii=False)
    assert "ark.example.invalid" not in json.dumps(rec, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 2. 200/type=message 仅 thinking 块 = 接受（G-12 豁免，单发不重试）
# ---------------------------------------------------------------------------

def test_thinking_only_200_accepted_single_send(env):
    calls = {"n": 0}

    def raw_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _resp(200, _message_body(
            blocks=[{"type": "thinking", "thinking": "隐式推理…"}],
            out_tok=9))

    gw = _gateway(env, raw_post)
    _approve("macro_ideation", BATCH_MACRO)
    with billing_context(task=6, batch_id=BATCH_MACRO, thread_id=THREAD):
        rec = gw.diag_thinking("macro_ideation")
    assert calls["n"] == 1  # G-12 豁免：thinking-only 200 绝不退避重试（不烧 5 seq）
    assert rec["accepted"] is True
    assert rec["schema"] == R.DIAG_THINKING_SCHEMA
    assert rec["http_status"] == 200 and rec["type"] == "message"
    assert rec["content_block_types"] == ["thinking"]
    assert rec["answer_head"] == ""
    assert rec["usage"]["output_tokens"] == 9
    rows = _int_rows(env["tmp"])
    assert len(rows) == 1 and rows[0]["ok"] is True
    assert rows[0]["stage"] == "macro_ideation" and rows[0]["task"] == 6
    assert rows[0]["batch_id"] == BATCH_MACRO and rows[0]["thread_id"] == THREAD
    assert rows[0]["tokens_out"] == 9


def test_thinking_plus_text_blocks_and_answer_head(env):
    def raw_post(url, headers=None, json=None, timeout=None):
        return _resp(200, _message_body(
            blocks=[{"type": "thinking", "thinking": "t"},
                    {"type": "text", "text": "1+1=2，答案是 2。" * 20}]))

    gw = _gateway(env, raw_post)
    _approve("coding_debug", BATCH_CODING)
    with billing_context(task=6, batch_id=BATCH_CODING):
        rec = gw.diag_thinking("coding_debug")
    assert rec["accepted"] is True
    assert rec["content_block_types"] == ["thinking", "text"]
    assert rec["requested_id"] == "glm-5.3-flash"
    assert rec["answer_head"].startswith("1+1=2") and len(rec["answer_head"]) <= 80


# ---------------------------------------------------------------------------
# 3. 明确 HTTP 拒绝（4xx/5xx/非 message）：不抛、accepted=False、一行 ok=false
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,body", [
    (400, {"type": "error",
           "error": {"type": "invalid_request_error", "code": "unknown_parameter",
                     "message": "unknown parameter: thinking"}}),
    (500, {"type": "error",
           "error": {"type": "service_unavailable", "code": "boom",
                     "message": "internal"}}),
    (200, {"type": "error", "error": {"type": "bad", "code": "x",
                                      "message": "200 但非 message 信封"}}),
])
def test_clear_http_rejection_no_raise_one_ok_false(env, status, body):
    calls = {"n": 0}

    def raw_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _resp(status, body)

    gw = _gateway(env, raw_post)
    _approve("macro_ideation", BATCH_MACRO)
    with billing_context(task=6, batch_id=BATCH_MACRO, thread_id=THREAD):
        rec = gw.diag_thinking("macro_ideation")  # 不抛异常
    assert calls["n"] == 1  # 任何明确 HTTP 响应都单发，无退避
    assert rec["accepted"] is False
    assert rec["http_status"] == status
    assert rec["err"] and f"http_status={status}" in rec["err"]
    assert len(rec["err"]) <= 300
    assert rec["body"]["type"] == "error"
    rows = _int_rows(env["tmp"])
    assert len(rows) == 1 and rows[0]["ok"] is False
    assert rows[0]["batch_id"] == BATCH_MACRO


# ---------------------------------------------------------------------------
# 4. 连接类异常：无 HTTP 证据，记一行 ok=false 后照抛
# ---------------------------------------------------------------------------

def test_connection_exception_recorded_then_raised(env):
    def raw_post(url, headers=None, json=None, timeout=None):
        raise requests.ConnectionError("dial tcp: connection refused")

    gw = _gateway(env, raw_post)
    _approve("coding_debug", BATCH_CODING)
    with billing_context(task=6, batch_id=BATCH_CODING, thread_id=THREAD):
        with pytest.raises(requests.ConnectionError):
            gw.diag_thinking("coding_debug")
    rows = _int_rows(env["tmp"])
    assert len(rows) == 1 and rows[0]["ok"] is False
    assert "ConnectionError" in rows[0]["err"]


# ---------------------------------------------------------------------------
# 5. 白名单/参数：preflight 之前拒绝，零触网零账行
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["visual_rollout", "planning", ""])
def test_stage_whitelist_value_error_no_network(env, stage):
    def must_not_call(*a, **k):
        raise AssertionError("白名单外 stage 禁止触网")

    gw = _gateway(env, must_not_call)
    _approve("macro_ideation", BATCH_MACRO)
    with billing_context(task=6, batch_id=BATCH_MACRO):
        with pytest.raises(ValueError):
            gw.diag_thinking(stage)
    assert _rows(env["tmp"]) == []


@pytest.mark.parametrize("max_tokens,budget", [
    (1024, 1024),   # 必须严格大于
    (512, 1024),    # budget 大于 max
    (0, 0),         # 非正
    (-1, -2),
    (2048.0, 1024), # 非 int
    (True, 1),      # bool 不算 int
])
def test_token_params_value_error_no_network(env, max_tokens, budget):
    def must_not_call(*a, **k):
        raise AssertionError("参数非法禁止触网")

    gw = _gateway(env, must_not_call)
    _approve("macro_ideation", BATCH_MACRO)
    with billing_context(task=6, batch_id=BATCH_MACRO):
        with pytest.raises(ValueError):
            gw.diag_thinking("macro_ideation", max_tokens=max_tokens,
                             budget_tokens=budget)
    assert _rows(env["tmp"]) == []


def test_null_stage_unresolved_no_network(env):
    def must_not_call(*a, **k):
        raise AssertionError("null stage 禁止触网")

    # 直接把 macro 置 null（带 open_question 锚点的配置由 validate 保证合法）。
    data = yaml.safe_load(Path(env["map"]).read_text(encoding="utf-8"))
    data["stage_models"]["macro_ideation"]["model_id"] = None
    data["stage_models"]["macro_ideation"]["open_question"] = "供应侧模型下线置 null 锚点"
    Path(env["map"]).write_text(yaml.safe_dump(data, allow_unicode=True),
                                encoding="utf-8")
    gw2 = ModelGateway(stage_models_path=env["map"], sleep=lambda s: None,
                       raw_transport=must_not_call, ledger=QuotaLedger())
    _approve("macro_ideation", BATCH_MACRO)
    with billing_context(task=6, batch_id=BATCH_MACRO):
        with pytest.raises(R.StageUnresolvedError):
            gw2.diag_thinking("macro_ideation")
    assert _rows(env["tmp"]) == []


# ---------------------------------------------------------------------------
# 6. 缺凭据：seq=null blocked，不占整数 seq（镜像 call_visual G-2）
# ---------------------------------------------------------------------------

def test_missing_credentials_blocked_row(env, monkeypatch):
    def must_not_call(*a, **k):
        raise AssertionError("缺凭据禁止触网")

    gw = _gateway(env, must_not_call)
    _approve("macro_ideation", BATCH_MACRO)
    monkeypatch.delenv("SIMPRAXIS_LLM_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with billing_context(task=6, batch_id=BATCH_MACRO, thread_id="t-g2"):
        with pytest.raises(R.MissingGatewayCredentialError):
            gw.diag_thinking("macro_ideation")
    assert _int_rows(env["tmp"]) == []
    blocked = [r for r in _rows(env["tmp"])
               if r.get("event") == "preflight_blocked"]
    assert len(blocked) == 1 and blocked[0]["seq"] is None
    assert blocked[0]["stage"] == "macro_ideation"
    assert "missing_gateway_credential" in blocked[0]["err"]


# ---------------------------------------------------------------------------
# 7. 源码红线：生产两传输路径 AST 级不含 thinking；diag 方法自身为正控
# ---------------------------------------------------------------------------

def _method_funcdef(class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == class_name)
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name == method_name)


def _thinking_keys(fn: ast.FunctionDef) -> list[str]:
    """收集函数体内 kwarg 名与 dict 字面量键中出现的 thinking。"""
    hits = []
    for node in ast.walk(fn):
        if isinstance(node, ast.keyword) and node.arg == "thinking":
            hits.append("keyword")
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and k.value == "thinking":
                    hits.append("dict-key")
    return hits


def test_redline_production_transports_carry_no_thinking():
    # D-8 旧红线：生产两传输路径 thinking 键 AST 级为零。
    # 攢批92（T8-D2 账外取证，2026-10-27）修订为分路径不变量：kimi-k3.1-pro
    # 在 shim 上强制思考（probe B 792s/0 正文；probe E disabled 后独白改走
    # text 通道 605.1s/126682 字截断），litellm 文本路径 _chat_loop 允许且
    # 只允许唯一钦点透传形态——thinking_kw 单键 dict 经 ** 展开进 completion，
    # 不得在 completion(...) 上直接写 thinking= 字面 kwarg（防止第二形态分叉）。
    loop = _method_funcdef("ModelGateway", "_chat_loop")
    assert _thinking_keys(loop) == ["dict-key"]
    starred = [kw for node in ast.walk(loop)
               if isinstance(node, ast.Call) for kw in node.keywords
               if kw.arg is None and isinstance(kw.value, ast.Name)
               and kw.value.id == "thinking_kw"]
    assert len(starred) == 1
    # _post_messages（validate/视觉 raw 通道）仍逐字节旧红线：零 thinking。
    assert _thinking_keys(_method_funcdef("ModelGateway", "_post_messages")) == []
    # 正控：扫描器本身有效——diag_thinking 体内必须能找到 thinking 键。
    assert _thinking_keys(_method_funcdef("ModelGateway", "diag_thinking"))


def test_diag_call_site_single_in_production_sources():
    callers: dict[str, int] = {}
    for root in ("orchestrator", "scripts"):
        for py in (_REPO_ROOT / root).rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            n = text.count(".diag_thinking(")
            if n:
                callers[str(py.relative_to(_REPO_ROOT))] = n
    # 全引擎/脚本侧唯一方法调用点就是探针脚本自身。
    assert callers == {"scripts/phase2/diag_thinking.py": 1}
    # 图/runner 任何文件连名字都不许出现（永不接线进生产流程）。
    for forbidden in (_REPO_ROOT / "orchestrator/graph").rglob("*.py"):
        assert "diag_thinking" not in forbidden.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 8. 脚本层：付费守卫 + 假网关离线落证据 + 0/10 分支
# ---------------------------------------------------------------------------

def _load_script():
    spec = importlib.util.spec_from_file_location(
        "phase2_diag_thinking", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeGateway:
    last_init = {}

    def __init__(self, *, ledger=None, **kw):
        type(self).last_init["ledger"] = ledger

    def diag_thinking(self, stage, *, max_tokens, budget_tokens):
        body = {"type": "message", "model": "fake",
                "content": [{"type": "thinking", "thinking": "x"},
                            {"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 3, "output_tokens": 2}}
        return {
            "schema": R.DIAG_THINKING_SCHEMA, "stage": stage,
            "requested_id": "fake-model", "echo_model": "fake",
            "http_status": 200, "type": "message", "latency_ms": 5,
            "request": {"keys": ["max_tokens", "messages", "model", "thinking"],
                        "max_tokens": max_tokens, "budget_tokens": budget_tokens},
            "content_block_types": ["thinking", "text"], "answer_head": "ok",
            "usage": {"input_tokens": 3, "output_tokens": 2,
                      "cache_read_input_tokens": None,
                      "cache_creation_input_tokens": None},
            "accepted": self.__class__.accepted, "err": None, "body": body}


class _FakeGatewayAccepted(_FakeGateway):
    accepted = True


class _FakeGatewayRejected(_FakeGateway):
    accepted = False

    def diag_thinking(self, stage, *, max_tokens, budget_tokens):
        rec = super().diag_thinking(stage, max_tokens=max_tokens,
                                    budget_tokens=budget_tokens)
        rec.update(http_status=400, type="error",
                   content_block_types=[], answer_head="",
                   err="http_status=400 | unknown_parameter | unknown parameter: thinking",
                   body={"type": "error",
                         "error": {"code": "unknown_parameter"}})
        return rec


def test_script_guard_bare_run_exits_2_zero_network(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    mod = _load_script()

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("守卫必须在构造网关之前 exit 2（零触网）")

    monkeypatch.setattr(mod, "ModelGateway", _Boom)
    rc = mod.main(["--stage", "macro_ideation", "--batch-id", BATCH_MACRO])
    assert rc == mod.EXIT_GUARD


def test_script_invalid_thread_batch_exits_2(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    mod = _load_script()
    out = tmp_path / "ev.json"
    rc = mod.main(["--stage", "macro_ideation", "--batch-id", "bad/batch",
                   "--thread", THREAD, "--out", str(out)])
    assert rc == mod.EXIT_GUARD and not out.exists()


def test_script_accepted_writes_0600_evidence_exit_0(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    mod = _load_script()
    monkeypatch.setattr(mod, "ModelGateway", _FakeGatewayAccepted)
    out = tmp_path / "diag.json"
    rc = mod.main(["--stage", "macro_ideation", "--batch-id", BATCH_MACRO,
                   "--thread", THREAD, "--out", str(out)])
    assert rc == 0 and out.is_file()
    assert out.stat().st_mode & 0o777 == 0o600
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is True and doc["stage"] == "macro_ideation"
    assert doc["batch_id"] == BATCH_MACRO and doc["thread_id"] == THREAD
    assert doc["body"]["type"] == "message"
    # 构造时必须挂真实账本（G-13），假的只是传输替身。
    assert isinstance(_FakeGateway.last_init["ledger"], QuotaLedger)


def test_script_rejected_writes_evidence_exit_10(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    mod = _load_script()
    monkeypatch.setattr(mod, "ModelGateway", _FakeGatewayRejected)
    out = tmp_path / "diag-reject.json"
    rc = mod.main(["--stage", "macro_ideation", "--batch-id", BATCH_MACRO,
                   "--out", str(out)])
    assert rc == mod.EXIT_REJECTED and out.is_file()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is False and doc["http_status"] == 400
    assert "unknown_parameter" in doc["err"]


def test_script_exception_exit_nonzero_no_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    mod = _load_script()

    class _BoomGateway:
        def __init__(self, *, ledger=None, **kw):
            pass

        def diag_thinking(self, *a, **k):
            raise requests.ConnectionError("refused")

    monkeypatch.setattr(mod, "ModelGateway", _BoomGateway)
    out = tmp_path / "nope.json"
    rc = mod.main(["--stage", "macro_ideation", "--batch-id", BATCH_MACRO,
                   "--out", str(out)])
    assert rc == mod.EXIT_ERROR and not out.exists()
