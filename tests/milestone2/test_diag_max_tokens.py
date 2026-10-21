"""max_tokens 上限接受性诊断探针的 hermetic 单测（0 付费、0 触网）。

覆盖（镜像 tests/milestone2/test_diag_thinking.py 脚本层纪律）：
- 裸跑付费守卫 exit 2 零触网；参数非法（票号/线程字符集、max_tokens
  区间、count_to 区间）exit 2 且在构造网关之前；
- **单发**：ModelGateway(max_attempts=1)，禁用 G-12 退避（一次运行至多
  一次付费 HTTP），由构造参数回归锁定；
- 接受路径：透传 stage=coding_debug / max_tokens 原值 / temperature=0 /
  timeout=TIMEOUT_S / 单 text block（永不携带 image，硬规则 1）；落 0600
  证据；headroom_proven/output_at_cap/数数核验字段；exit 0；
- usage 为 None（网关合法契约）不得崩在证据写入之前；
- 明确拒绝（NonRetryableError，如 400）：落 accepted=false 脱敏证据
  exit 10；证据写盘失败 exit 1；
- 连接类异常：exit 1，不落证据；
- 红线：orchestrator/ 与 scripts/ 中除探针脚本自身外任何文件不得出现
  diag_max_tokens 名字（图/runner 永不接线）；脚本不得 import
  orchestrator.graph（ast.Import/ImportFrom 双查）；不提供模型 ID 参数。
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest
import requests

import orchestrator.gateway.router as R
from orchestrator.gateway.quota_ledger import QuotaLedger

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _REPO_ROOT / "scripts" / "phase2" / "diag_max_tokens.py"
BATCH = "T6-B2-probe32k"
THREAD = "t6-genesis-ascent-20261016a"
# 非真密钥形态（scan_secrets 严口径不命中），仅作证据不回显凭据的哨兵。
TOKEN = "diag-max-tokens-test-key-redacted-0001"
# Bearer 宽脱敏形态：redact_text 必剥（redact_only），严口径扫描不命中。
BEARER_SECRET = "Bearer diagmaxtokentestredacted0001"
HOST = "ark.example.invalid"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    # 假网关吃掉全部网络，凭据/URL 仅用于断言证据不回显。
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", TOKEN)
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL", f"https://{HOST}/api/coding")
    monkeypatch.setenv("SIMPRAXIS_PHASE1_ROOT", str(tmp_path / "phase1"))
    monkeypatch.setenv("SIMPRAXIS_PHASE2_ROOT", str(tmp_path / "phase2"))
    monkeypatch.setenv("SIMPRAXIS_GATEWAY_STATE", str(tmp_path / "gwstate"))
    return tmp_path


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "phase2_diag_max_tokens", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _numbers_text(n):
    return "\n".join(str(i) for i in range(1, n + 1))


class _FakeGateway:
    """替身只实现 call()：记录透传参数，返回 (text, (in_tok, out_tok))。"""
    seen: dict = {}
    out_tokens = 17001
    in_tokens = 31
    raises: Exception | None = None
    text: str | None = None
    count_n = 200

    def __init__(self, *, ledger=None, **kw):
        type(self).seen["ledger"] = ledger
        type(self).seen["init_kwargs"] = kw

    def call(self, stage, messages, *, max_tokens, temperature, timeout):
        type(self).seen.update(
            stage=stage, messages=messages, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout)
        if self.raises is not None:
            raise self.raises
        return (self.text if self.text is not None
                else _numbers_text(self.count_n)), (self.in_tokens,
                                                    self.out_tokens)


def _fake(*, out_tokens=17001, exc=None, text=None, count_n=200):
    return type("FG", (_FakeGateway,),
                {"out_tokens": out_tokens, "raises": exc, "seen": {},
                 "text": text, "count_n": count_n})


# ---------------------------------------------------------------------------
# 1. 付费守卫 + 参数校验：裸跑/非法参数零触网 exit 2
# ---------------------------------------------------------------------------

def test_script_guard_bare_run_exits_2_before_gateway(monkeypatch):
    monkeypatch.delenv("SIMPRAXIS_ALLOW_PAID", raising=False)
    mod = _load_script()

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("守卫必须在构造网关之前 exit 2（零触网）")

    monkeypatch.setattr(mod, "ModelGateway", _Boom)
    rc = mod.main(["--batch-id", BATCH])
    assert rc == mod.EXIT_GUARD


@pytest.mark.parametrize("argv", [
    ["--batch-id", "bad/batch"],                       # 票号字符集
    ["--batch-id", BATCH, "--max-tokens", "16384"],    # 下界：等于现网即非法
    ["--batch-id", BATCH, "--max-tokens", "70000"],    # 超探针上限 65536
    ["--batch-id", BATCH, "--count-to", "50"],         # count 低于 100
    ["--batch-id", BATCH, "--count-to", "100001"],     # count 超上限
    ["--batch-id", BATCH, "--thread", "bad thread"],   # 线程字符集
])
def test_script_invalid_args_exits_2(monkeypatch, env, argv):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("参数非法必须在构造网关之前 exit 2")

    monkeypatch.setattr(mod, "ModelGateway", _Boom)
    assert mod.main(argv) == mod.EXIT_GUARD


# ---------------------------------------------------------------------------
# 2. 接受路径：单发、透传契约、0600 证据、核验字段、exit 0
# ---------------------------------------------------------------------------

def test_script_accepted_single_send_full_evidence(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=17001)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag.json"
    rc = mod.main(["--batch-id", BATCH, "--thread", THREAD,
                   "--max-tokens", "32768", "--out", str(out)])
    assert rc == 0 and out.is_file()
    assert out.stat().st_mode & 0o777 == 0o600
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is True and doc["probe"] == "diag_max_tokens"
    assert doc["headroom_proven"] is True
    assert doc["stage"] == "coding_debug" and doc["max_tokens"] == 32768
    assert doc["batch_id"] == BATCH and doc["thread_id"] == THREAD
    assert doc["count_to"] == 12000
    assert doc["usage"] == {"input_tokens": 31, "output_tokens": 17001}
    # 数数核验：假文本只数到 200，未完成但为有序前缀；未贴 32768 线。
    assert doc["count_completed"] is False
    assert doc["ordered_prefix"] is True
    assert doc["last_number"] == 200
    assert doc["numbers_parsed"] == 200 and doc["count_lines"] == 200
    assert doc["output_at_cap"] is False
    # 透传契约：stage 恒 coding_debug / max_tokens 原值 / temperature=0。
    seen = fake.seen
    assert seen["stage"] == "coding_debug"
    assert seen["max_tokens"] == 32768 and seen["temperature"] == 0
    assert seen["timeout"] == mod.TIMEOUT_S
    # M1 回归：单发（max_attempts=1），G-12 退避不得在探针里复活。
    assert seen["init_kwargs"]["max_attempts"] == 1
    # 消息体：单 user text block，绝无 image（硬规则 1）。
    msgs = seen["messages"]
    assert "image" not in json.dumps(msgs)
    assert msgs == [{"role": "user", "content": [
        {"type": "text", "text": msgs[0]["content"][0]["text"]}]}]
    assert msgs[0]["content"][0]["text"]
    # 构造挂真实账本（G-13：账外构造 fail-closed），假的只是传输替身。
    assert isinstance(fake.seen["ledger"], QuotaLedger)
    # 证据不回显凭据/主机名；原文只留长度与脱敏 head/tail 200。
    blob = out.read_text(encoding="utf-8")
    assert TOKEN not in blob and HOST not in blob
    assert len(doc["raw_head"]) <= 200 and len(doc["raw_tail"]) <= 200
    assert doc["raw_chars"] == len(_numbers_text(200))


def test_script_nondefault_max_tokens_passthrough_and_at_cap(monkeypatch, env):
    # n1：非默认值必须透传，且贴线判定 output_at_cap=true。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=20000)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-20k.json"
    rc = mod.main(["--batch-id", BATCH, "--max-tokens", "20000",
                   "--out", str(out)])
    assert rc == 0
    assert fake.seen["max_tokens"] == 20000
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["max_tokens"] == 20000
    assert doc["output_at_cap"] is True
    assert doc["headroom_proven"] is True


def test_script_boundary_16385_accepted(monkeypatch, env):
    # n2：探针下界 16385 合法且单发成功。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=17001)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-16385.json"
    rc = mod.main(["--batch-id", BATCH, "--max-tokens", "16385",
                   "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is True and doc["max_tokens"] == 16385
    assert fake.seen["max_tokens"] == 16385


@pytest.mark.parametrize("out_tokens,proven", [(16384, False), (12000, False)])
def test_script_headroom_proven_false_at_or_below_wire(monkeypatch, env,
                                                       out_tokens, proven):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=out_tokens)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-small.json"
    rc = mod.main(["--batch-id", BATCH, "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is True and doc["headroom_proven"] is proven
    assert doc["output_at_cap"] is False


def test_script_count_completed_exact_sequence(monkeypatch, env):
    # M2：假文本恰好 1..200 + --count-to 200 → count_completed=true。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=17001, count_n=200)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-complete.json"
    rc = mod.main(["--batch-id", BATCH, "--count-to", "200",
                   "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["count_to"] == 200
    assert doc["count_completed"] is True
    assert doc["ordered_prefix"] is True
    assert (doc["numbers_parsed"], doc["count_lines"],
            doc["last_number"]) == (200, 200, 200)


def test_script_count_rejects_gappy_or_restarted_sequence(monkeypatch, env):
    # M2 旁证质量：跳号/重数必须既不算完成也不算有序前缀。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=500, text="1\n2\n4\n5\n1\n2", count_n=0)
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-gap.json"
    rc = mod.main(["--batch-id", BATCH, "--count-to", "200",
                   "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["numbers_parsed"] == 6 and doc["last_number"] == 2
    assert doc["count_completed"] is False
    assert doc["ordered_prefix"] is False


def test_script_repeated_plus_line_parsed_same_as_guard(monkeypatch, env):
    # MINOR-A 回归：守卫 s.lstrip("+").isdigit() 与 int() 必须同一口径，
    # "++5" 这类行不得在已付费的接受路径上把证据构建打崩。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    check = mod._count_check("1\n++5\n3", 200)
    assert check["numbers_parsed"] == 3 and check["last_number"] == 3
    assert check["ordered_prefix"] is False  # 1,5,3 不是 1.. 前缀，但不抛异常
    # 纯函数直接断言：++5 按守卫语义解析为 5。
    assert mod._count_check("++5\n", 200)["numbers_parsed"] == 1


def test_script_accepted_evidence_write_failure_exit_1(monkeypatch, env):
    # NIT 对称回归：接受路径证据写盘 OSError 也走 exit 1，不 traceback。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=17001)
    monkeypatch.setattr(mod, "ModelGateway", fake)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "atomic_write_text", _boom)
    rc = mod.main(["--batch-id", BATCH, "--out", str(env / "d.json")])
    assert rc == mod.EXIT_ERROR


def test_script_none_usage_tokens_still_writes_evidence(monkeypatch, env):
    # m1：usage.output_tokens=None（网关合法契约）不得在写证据前 TypeError。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(out_tokens=None, text=f"see https://{HOST}/secret-path\n1\n2")
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-none.json"
    rc = mod.main(["--batch-id", BATCH, "--count-to", "200",
                   "--out", str(out)])
    assert rc == 0 and out.is_file()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["accepted"] is True
    assert doc["usage"] == {"input_tokens": 31, "output_tokens": None}
    assert doc["headroom_proven"] is False and doc["output_at_cap"] is False
    assert doc["last_number"] == 2
    # m3：raw 文本里的 URL 也必须在 head/tail 中被结构化剥离。
    assert HOST not in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. 明确拒绝（NonRetryableError，如供应侧 400）：脱敏证据 exit 10
# ---------------------------------------------------------------------------

def test_script_rejected_writes_redacted_evidence_exit_10(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(exc=R.NonRetryableError(
        f"400 invalid_request_error: max_tokens too large; docs "
        f"https://{HOST}/api/coding/v1/messages {BEARER_SECRET}"))
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "diag-reject.json"
    rc = mod.main(["--batch-id", BATCH, "--out", str(out)])
    assert rc == mod.EXIT_REJECTED and out.is_file()
    assert out.stat().st_mode & 0o777 == 0o600
    blob = out.read_text(encoding="utf-8")
    doc = json.loads(blob)
    assert doc["accepted"] is False and doc["max_tokens"] == 32768
    assert doc["count_to"] == 12000 and "max_tokens" in doc["err"]
    assert len(doc["err"]) <= 300
    # m3：拒绝证据同样不得回显主机名/凭据（litellm 异常串可能带 URL）。
    assert HOST not in blob and "diagmaxtokentestredacted0001" not in blob
    assert "[url]" in doc["err"] and "Bearer" in doc["err"]  # 前缀保留、本体剥除


def test_script_rejected_evidence_write_failure_exit_1(monkeypatch, env):
    # n5：seq 已记，证据写盘 OSError 不得 traceback，走 exit 1。
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(exc=R.NonRetryableError("400 max_tokens too large"))
    monkeypatch.setattr(mod, "ModelGateway", fake)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "atomic_write_text", _boom)
    rc = mod.main(["--batch-id", BATCH, "--out", str(env / "x" / "d.json")])
    assert rc == mod.EXIT_ERROR


# ---------------------------------------------------------------------------
# 4. 连接类异常：exit 1，不落证据
# ---------------------------------------------------------------------------

def test_script_exception_exit_1_no_evidence(monkeypatch, env):
    monkeypatch.setenv("SIMPRAXIS_ALLOW_PAID", "1")
    mod = _load_script()
    fake = _fake(exc=requests.ConnectionError("refused"))
    monkeypatch.setattr(mod, "ModelGateway", fake)
    out = env / "nope.json"
    rc = mod.main(["--batch-id", BATCH, "--out", str(out)])
    assert rc == mod.EXIT_ERROR and not out.exists()


# ---------------------------------------------------------------------------
# 5. 源码红线：唯一提及点、图永不接线、不 import graph、无模型覆盖参数
# ---------------------------------------------------------------------------

def test_redline_call_site_single_and_graph_unaware():
    mentions: set[str] = set()
    for root in ("orchestrator", "scripts"):
        for py in (_REPO_ROOT / root).rglob("*.py"):
            if "diag_max_tokens" in py.read_text(encoding="utf-8"):
                mentions.add(str(py.relative_to(_REPO_ROOT)))
    # 引擎/脚本中只有探针脚本自身提及该名（测试文件不在扫描根内）。
    assert mentions == {"scripts/phase2/diag_max_tokens.py"}
    # graph/ 与 runner 任何文件连名字都不许出现（永不接线进生产流程）。
    for forbidden in list((_REPO_ROOT / "orchestrator/graph").rglob("*.py")) + \
            [_REPO_ROOT / "orchestrator/agent_runner/runner.py"]:
        assert "diag_max_tokens" not in forbidden.read_text(encoding="utf-8")
    # 脚本自身不反向 import 图（AST 级：docstring 说明不算；
    # n3：ast.Import 与 ast.ImportFrom 双查）。
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    from_modules = {n.module for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) and n.module}
    plain_modules = {".".join(a.name.split(".")[:2]) for n in ast.walk(tree)
                     if isinstance(n, ast.Import) for a in n.names}
    assert not any(m == "orchestrator.graph"
                   or (m or "").startswith("orchestrator.graph.")
                   for m in from_modules | plain_modules)
    # 不提供任何模型 ID 覆盖入口（硬规则 1：模型只能读 stage_models.yaml）。
    flags = {kw.value for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "add_argument"
             for kw in n.args if isinstance(kw, ast.Constant)}
    assert "--model" not in flags and "--model-id" not in flags
