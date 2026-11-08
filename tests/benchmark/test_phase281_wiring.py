"""P281-3 机判（AC-P281-3-01..10；0 付费、0 联网）。

本 Task 只做接线，判据全部是「行为 + 源码」两段，不读生产账：

* AC-01：``run_agentic_for_runspec`` 新增 ``ledger`` 关键字，缺省 ``None``；
* AC-02：显式账本 + ``call_cache=None`` ⇒ ``ValueError``（封堵跨 Trial 复用）；
* AC-03：两者皆 ``None`` ⇒ 不抛，仍走 ``phase1_root()/.call_cache`` 旧路径；
* AC-04：传入的 ledger 原对象透传给 ``build_seq_provider``；
* AC-05：``build_seq_provider()`` 缺省回退 **phase281**（源码 + 行为双证）；
* AC-06：源码不再出现 ``QuotaLedger.phase27()`` 字面；
* AC-07：两个 Trial 的 ``CallReplay`` 目录不同且互不命中；
* AC-08：``max_attempts=1`` 的网关在注入 500 上**只发一次** HTTP；
* AC-09：一次失败调用恰一行 ``ok=false``，两次则 seq 连续 1,2；
* AC-10：``billing_context`` 签名未变、``quota_ledger.py`` 相对 P281-2 零改动、
  且 ``ModelGateway(ledger=None)`` 不带 ``unguarded`` 时构造即抛。

纪律：全部传输走 monkeypatch/假响应；``phase281`` 生产账本不参与（fixture 把六根
全指 tmp）。账本写入只发生在 tmp 根，用来验 seq 语义。
"""
from __future__ import annotations

import inspect
import json
import subprocess
import types
import yaml
from pathlib import Path

import pytest

import orchestrator.agentic.wiring as W
import orchestrator.gateway.router as R
from orchestrator.agentic.loop import AgenticOutcome
from orchestrator.common.callcache import CallReplay
from orchestrator.common import runtime_paths as rp  # 锁内适配（RSI-R0 T1）
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import QuotaLedger, billing_context

REPO_ROOT = Path(__file__).resolve().parents[2]

#: P281-2 的提交（不可变父提交）。AC-10 拿它当「本 Task 之前」的基线：
#: ``quota_ledger.py`` 在本 Task 里不许动一个字节。
P281_2_HEAD = "a8011672ad8016fe838977e5ebc2a3451e5e4898"
#: Phase 2.8.1 结案提交（不可变；AC-10 区间右端，见下）。
P281_SEAL_HEAD = "7766342"

STAGE = "coding_debug"
BATCH = "P281-T3-FIXTURE"
ANCHOR = ("供应侧若下线该阶段模型，执行期以 --validate-models 实呼为准，"
          "未恢复前该 stage 置 null")
DEFAULT_IDS = {
    "macro_ideation": "deepseek-v4.1-flash",
    "coding_debug": "glm-5.3-flash",
    "visual_rollout": "ark-code-latest",
}
TEXT_MSG = [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def roots(tmp_path, monkeypatch):
    """六根全指 tmp（含 phase281），凭据用 redacted 形态串（B2：不像真密钥）。"""
    for env, name in (("SIMPRAXIS_PHASE1_ROOT", "phase1"),
                      ("SIMPRAXIS_PHASE2_ROOT", "phase2"),
                      ("SIMPRAXIS_PHASE25_ROOT", "phase25"),
                      ("SIMPRAXIS_PHASE26_ROOT", "phase26"),
                      ("SIMPRAXIS_PHASE27_ROOT", "phase27"),
                      ("SIMPRAXIS_PHASE281_ROOT", "phase281"),
                      # 锁内适配（RSI-R0 T1）：活跃写账换成 rsi_r0 campaign。
                      ("SIMPRAXIS_RSI_R0_ROOT", "r0root"),
                      ("SIMPRAXIS_GATEWAY_STATE", "gwstate"),
                      ("SIMPRAXIS_RUN_DIR", "runs")):
        monkeypatch.setenv(env, str(tmp_path / name))
    monkeypatch.setenv("SIMPRAXIS_RSI_R0_CAMPAIGN", "r0test")  # 锁内适配（RSI-R0 T1）
    monkeypatch.setenv("SIMPRAXIS_LLM_TOKEN", "wiring-test-key-redacted-0001")
    monkeypatch.setenv("SIMPRAXIS_LLM_BASE_URL",
                       "https://ark.cn-beijing.volces.com/api/coding")
    oq = tmp_path / "open-questions.md"
    oq.write_text(f"# Open Questions\n\n- {ANCHOR}\n", encoding="utf-8")
    monkeypatch.setenv("SIMPRAXIS_OPEN_QUESTIONS", str(oq))
    monkeypatch.delenv("SIMPRAXIS_BENCHMARK_ROOT", raising=False)
    return tmp_path


def _spec(workdir: Path, *, grants=("finish", "fail")):
    """最小 agentic RunSpec 鸭子对象（只给 wiring 真读的字段）。"""
    return types.SimpleNamespace(
        agentic=True,
        task_title="P281-3 fixture",
        cmd=("python", "solution.py"),
        workdir=str(workdir),
        thread_id="thr-t3",
        telemetry_relpath="telemetry.jsonl",
        agentic_trace_relpath=None,
        tool_grants=tuple(grants),
        agentic_config=None,
        step_cap=12, token_cap=40000, max_coding_rounds=2,
        gate2_notes=None,
    )


def _stub_inner(monkeypatch, *, captured: dict | None = None):
    """把内环替换成记录参数并立刻自终局的桩（不起 smolagents、不触网）。"""
    def _fake(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return AgenticOutcome(kind="finish", value="done")
    monkeypatch.setattr(W, "run_agentic_inner_loop", _fake)


def _write_map(path: Path) -> None:
    sm = {s: {"model_id": DEFAULT_IDS[s],
              "capabilities": ["text", "vision"] if s == R.VISION_STAGE else ["text"],
              "purpose": "t"} for s in R.STAGES}
    path.write_text(yaml.safe_dump({
        "meta": {"schema_version": "1.0", "endpoint": "anthropic-messages",
                 "base_url_env": "ANTHROPIC_BASE_URL"},
        "stage_models": sm,
    }, allow_unicode=True, width=4096), encoding="utf-8")


def _approve(approved: int = 8) -> None:
    ql.write_approval(batch_id=BATCH, task=1, stage=STAGE, approved=approved,
                      beyond_normal=False, note="P281-3 fixture（零付费单测）",
                      namespace=ql.RSI_R0_NAMESPACE)  # 锁内适配（RSI-R0 T1）


def _ledger_rows() -> "list[dict]":
    path = rp.rsi_r0_ledger_path()  # 锁内适配（RSI-R0 T1）
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _int_rows() -> "list[dict]":
    return [r for r in _ledger_rows() if isinstance(r.get("seq"), int)]


def _five_hundred(calls: list):
    """注入 5xx 的 **litellm 替身**（``transport=``）。

    注意：文本入口 ``call()`` 走的是 ``self._transport or litellm.completion``——
    ``raw_transport`` 只服务 ``call_visual``/``ping_text``/``validate_models``。
    因此验证「单次 HTTP」必须注入 ``transport``；且**不得**让 ``transport`` 为
    None（那会落到真 ``litellm.completion``，即真网）。
    """
    from litellm.exceptions import ServiceUnavailableError

    def _boom(**kwargs):
        calls.append(kwargs)
        raise ServiceUnavailableError("500 upstream boom", "anthropic",
                                      "glm-5.3-flash", None)
    return _boom


# ---------------------------------------------------------------------------
# AC-P281-3-01：签名含 ledger 关键字，缺省 None
# ---------------------------------------------------------------------------

def test_ac_p281_3_01_signature():
    sig = inspect.signature(W.run_agentic_for_runspec)
    params = sig.parameters
    assert "ledger" in params, sorted(params)
    ledger = params["ledger"]
    assert ledger.kind is inspect.Parameter.KEYWORD_ONLY, ledger.kind
    assert ledger.default is None, ledger.default
    # 既有关键字一个不少、缺省一律不变（缺省行为不变的签名面证明）
    for name in ("recall_port", "run_backend", "probe_registry", "call_cache",
                 "nonprod"):
        assert name in params and params[name].default in (None, False), name


# ---------------------------------------------------------------------------
# AC-P281-3-02：显式账本 + 无隔离缓存 ⇒ ValueError
# ---------------------------------------------------------------------------

def test_ac_p281_3_02_explicit_ledger_requires_cache(roots, tmp_path):
    spec = _spec(tmp_path / "wd")
    with pytest.raises(ValueError) as excinfo:
        W.run_agentic_for_runspec(spec, gateway=None,
                                  ledger=QuotaLedger.phase281())
    assert "call_cache" in str(excinfo.value), str(excinfo.value)
    # 守卫在阶段入口：连内环都没进（桩没被调用也无所谓——这里显式断言零产物）
    assert not (tmp_path / "wd").exists() or not any(
        (tmp_path / "wd").iterdir())


# ---------------------------------------------------------------------------
# AC-P281-3-03：两者皆 None ⇒ 不抛，仍走 phase1_root()/.call_cache
# ---------------------------------------------------------------------------

def test_ac_p281_3_03_default_path_unchanged(roots, tmp_path, monkeypatch):
    captured: dict = {}
    _stub_inner(monkeypatch, captured=captured)
    monkeypatch.setattr(W, "phase1_root", lambda: tmp_path / "phase1")

    out = W.run_agentic_for_runspec(_spec(tmp_path / "wd"), gateway=object())
    assert out["status"] == "ok", out
    cache = captured["call_cache"]
    assert isinstance(cache, CallReplay)
    assert cache.dir == (tmp_path / "phase1" / ".call_cache").resolve(), cache.dir
    # 缺省 ledger 也照旧透传 None（不触发任何换期写）
    assert captured["seq_provider"] is not None


def test_ac_p281_3_03b_explicit_cache_is_used(roots, tmp_path, monkeypatch):
    """显式缓存照用（对照组：证明 -03 观察的是缺省分支而不是「总用 tmp」）。"""
    captured: dict = {}
    _stub_inner(monkeypatch, captured=captured)
    mine = CallReplay(tmp_path / "trial-a" / "cache")
    W.run_agentic_for_runspec(_spec(tmp_path / "wd"), gateway=object(),
                              call_cache=mine)
    assert captured["call_cache"] is mine


# ---------------------------------------------------------------------------
# AC-P281-3-04：ledger 原对象透传给 build_seq_provider
# ---------------------------------------------------------------------------

def test_ac_p281_3_04_ledger_threaded(roots, tmp_path, monkeypatch):
    captured: dict = {}
    _stub_inner(monkeypatch, captured=captured)
    seen: dict = {}

    def _probe(*, ledger=None):
        seen["ledger"] = ledger
        return lambda: None

    monkeypatch.setattr(W, "build_seq_provider", _probe)
    ledger = QuotaLedger.phase281()
    W.run_agentic_for_runspec(
        _spec(tmp_path / "wd"), gateway=object(),
        call_cache=CallReplay(tmp_path / "trial-a" / "cache"),
        ledger=ledger)
    assert seen["ledger"] is ledger, seen


# ---------------------------------------------------------------------------
# AC-P281-3-05：build_seq_provider() 缺省回退 phase281
# 锁内适配（RSI-R0 T1）：phase281 已随 2.8.1 HOLD 结案封存，缺省账再换期到
# rsi_r0（同一条「读取器必须盯活跃账」的教训，第四次换期）；断言形态不变：
# 源码里必须是当前活跃账的构造函数，且旧期不得出现。
# ---------------------------------------------------------------------------

def test_ac_p281_3_05_seq_default_phase281(roots):
    src = inspect.getsource(W.build_seq_provider)
    assert "QuotaLedger.rsi_r0()" in src, src
    assert "QuotaLedger.phase281()" not in src, src
    assert "QuotaLedger.phase27()" not in src, src

    # 行为：无 billing 上下文 ⇒ None（零 IO 的既有语义）
    assert W.build_seq_provider()() is None

    _approve()
    led = QuotaLedger.rsi_r0()
    with billing_context(task=1, batch_id=BATCH, thread_id="thr-t3"):
        rec = led.record(STAGE, task=1, batch_id=BATCH, ok=True,
                         model="fixture-model", thread_id="thr-t3")
        assert rec["seq"] == 1
        # 缺省读取器盯的就是 rsi_r0 账：读得到这一行（RSI-R0 T1 换期）
        assert W.build_seq_provider()() == 1
        # 显式别的期读不到（证明 -05 的前提成立：这是换期的效果而非巧合）
        assert W.build_seq_provider(ledger=QuotaLedger.phase27())() is None
        assert W.build_seq_provider(ledger=QuotaLedger.phase281())() is None
    # 上下文退出后回到 None
    assert W.build_seq_provider()() is None


# ---------------------------------------------------------------------------
# AC-P281-3-06：wiring.py 不再出现 QuotaLedger.phase27() 字面
# ---------------------------------------------------------------------------

def test_ac_p281_3_06_no_stale_phase27():
    assert "QuotaLedger.phase27()" not in inspect.getsource(W)


# ---------------------------------------------------------------------------
# AC-P281-3-07：两个 Trial 的 CallReplay 目录不同且互不命中
# ---------------------------------------------------------------------------

def test_ac_p281_3_07_cache_isolation(tmp_path):
    a = CallReplay(tmp_path / "trial-a" / "cache")
    b = CallReplay(tmp_path / "trial-b" / "cache")
    assert a.dir != b.dir
    a.put("agentic-v4-key-0001", kind="text", model="m", raw="A-RESULT",
          usage=(3, 4), fingerprint="fp-1")
    assert a.get("agentic-v4-key-0001", "fp-1")["raw"] == "A-RESULT"
    assert b.get("agentic-v4-key-0001", "fp-1") is None


# ---------------------------------------------------------------------------
# AC-P281-3-08 / -09：单次 HTTP；失败仍占 seq
# ---------------------------------------------------------------------------

def test_ac_p281_3_08_single_attempt(roots, tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    _approve()
    calls: list = []
    gw = R.ModelGateway(stage_models_path=map_path,
                        transport=_five_hundred(calls), max_attempts=1,
                        sleep=lambda s: None, ledger=QuotaLedger.rsi_r0())
    with billing_context(task=1, batch_id=BATCH, thread_id="thr-t3"):
        with pytest.raises(R.GatewayError):
            gw.call(STAGE, TEXT_MSG, max_tokens=64)
    assert len(calls) == 1, f"max_attempts=1 却发了 {len(calls)} 次 HTTP"
    # 缺省值仍是 5（router 一字未改）：只有实验侧显式传 1 才会变成单次
    assert inspect.signature(R.ModelGateway.__init__).parameters[
        "max_attempts"].default == 5


def test_ac_p281_3_09_failed_call_still_recorded(roots, tmp_path):
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    _approve()
    calls: list = []
    gw = R.ModelGateway(stage_models_path=map_path,
                        transport=_five_hundred(calls), max_attempts=1,
                        sleep=lambda s: None, ledger=QuotaLedger.rsi_r0())
    with billing_context(task=1, batch_id=BATCH, thread_id="thr-t3"):
        for _ in range(2):
            with pytest.raises(R.GatewayError):
                gw.call(STAGE, TEXT_MSG, max_tokens=64)
    # 两次逻辑调用 = 两次传输尝试（每次 max_attempts=1），且都确实发到了传输层
    assert len(calls) == 2, len(calls)

    rows = _int_rows()
    assert [r["seq"] for r in rows] == [1, 2], rows          # 连续、不跳号
    assert [r["ok"] for r in rows] == [False, False], rows   # 失败不冒功
    assert all(r["event"] == "call" for r in rows), rows
    assert all(r["thread_id"] == "thr-t3" for r in rows), rows
    # 账本自身机判口径也过（seq 1..n 连续 + 严格 Z）
    ql.validate_ledger_rows(_ledger_rows())


# ---------------------------------------------------------------------------
# AC-P281-3-10：billing_context 未动、quota_ledger.py 零改动、无账本不可构造
# ---------------------------------------------------------------------------

def test_ac_p281_3_10_billing_context_untouched(roots, tmp_path):
    sig = inspect.signature(billing_context)
    params = sig.parameters
    assert list(params) == ["task", "batch_id", "thread_id"], list(params)
    for name in ("task", "batch_id"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is inspect.Parameter.empty, name
    assert params["thread_id"].default == "", params["thread_id"].default
    assert "ledger" not in params

    # 账本不在 BillingContext 里（v0.1 的「billing_context 加成 ledger 参数」是误判）
    fields = tuple(ql.BillingContext.__dataclass_fields__)
    assert fields == ("task", "batch_id", "thread_id", "evidence"), fields

    # quota_ledger.py 相对 P281-2 零改动。
    # 锁内适配（RSI-R0 T1）：区间右端由「活工作树」钉成**不可变提交**
    # ``P281_SEAL_HEAD``（2.8.1 结案提交 7766342 = R0 分支的父提交）。原写法拿
    # 工作树比，R0 按计划给 quota_ledger.py 加 rsi_r0 命名空间后这条历史断言
    # 就会误红；钉死端点后强度不变（仍是「本 Task 前后零改动」），且不再被
    # 后续阶段的合法改动影响。
    diff = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--name-only", P281_2_HEAD,
         P281_SEAL_HEAD, "--", "orchestrator/gateway/quota_ledger.py"],
        text=True, capture_output=True, check=False)
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == "", diff.stdout

    # 忘挂账本 = 构造期错误（不是运行期漏账）
    map_path = tmp_path / "stage_models.yaml"
    _write_map(map_path)
    with pytest.raises(R.GatewayError, match="QuotaLedger"):
        R.ModelGateway(stage_models_path=map_path)
    with pytest.raises(R.GatewayError, match="QuotaLedger"):
        R.ModelGateway(stage_models_path=map_path, ledger=None)
    assert R.ModelGateway(stage_models_path=map_path,
                          unguarded=True)._billing is None
