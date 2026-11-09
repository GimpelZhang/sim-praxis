"""付费受闸通道单测（reviewer MEDIUM-4）：零网络、零付费。

证明 ``PaidTransport`` 是生产调用点：闸失败时 sender 一次都不被调用，
闸通过时恰好一次 POST、attempt 记账。
"""
from __future__ import annotations

import pytest

from orchestrator.rsi_r01 import lock as L
from orchestrator.rsi_r01 import paid as P
from orchestrator.rsi_r01 import transport as T


def _plan() -> T.SendPlan:
    return T.plan_from_messages(
        system=None, messages=[{"role": "user", "content": "ping"}],
        model="test-model")


def _ok_sender(calls: "list[T.SendPlan]"):
    def sender(plan: T.SendPlan):
        calls.append(plan)
        return {"content_blocks": [{"type": "text", "text": "pong"}],
                "http_status": 200}, "pong"
    return sender


def test_gate_failure_blocks_before_any_send(monkeypatch):
    def boom(ctx, repo_root=L.REPO_ROOT):
        raise L.LockError("LOCK_CHANGED", ["事实已变"])

    monkeypatch.setattr(L, "assert_paid_allowed", boom)
    transport = P.PaidTransport(ctx=None)
    plan = _plan()
    calls: "list[T.SendPlan]" = []
    with pytest.raises(L.LockError):
        transport.send(plan, T.count_input(plan), slot_id="slot-1",
                       sender=_ok_sender(calls))
    assert calls == []            # 一个字节都没发
    assert transport.posts == 0
    assert transport.attempts == []


def test_gate_pass_sends_exactly_once(monkeypatch):
    monkeypatch.setattr(L, "assert_paid_allowed",
                        lambda ctx, repo_root=L.REPO_ROOT: {"ok": True})
    transport = P.PaidTransport(ctx=None)
    plan = _plan()
    calls: "list[T.SendPlan]" = []
    att = transport.send(plan, T.count_input(plan), slot_id="slot-1",
                         sender=_ok_sender(calls))
    assert len(calls) == 1 and transport.posts == 1
    assert att.state == T.SendState.SENT_OK
    assert att.request_sha256 == plan.sha256


def test_gate_is_rechecked_for_every_post(monkeypatch):
    seen = {"n": 0}

    def gate(ctx, repo_root=L.REPO_ROOT):
        seen["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(L, "assert_paid_allowed", gate)
    transport = P.PaidTransport(ctx=None)
    plan = _plan()
    calls: "list[T.SendPlan]" = []
    for _ in range(3):
        transport.send(plan, T.count_input(plan), slot_id="slot-x",
                       sender=_ok_sender(calls))
    assert seen["n"] == 3 and transport.posts == 3   # 每 POST 前都重算


# ---- rev 3（M-2-a）：底层 Transport 私有化 + 静态检查 ---------------------

def _source_scan(root):
    """扫 orchestrator/rsi_r01 与 scripts/rsi_r01 下所有 .py 源码。"""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    paths = sorted((repo / "orchestrator/rsi_r01").glob("*.py")) + \
        sorted((repo / "scripts/rsi_r01").glob("*.py"))
    return {p: p.read_text(encoding="utf-8") for p in paths if p.name != "__init__.py"}


def test_paid_transport_has_no_public_inner():
    """底层 Transport 只能私有：公开属性会被绕过闸直接使用（M-2-a）。"""
    assert not hasattr(P.PaidTransport(ctx=None), "inner")
    assert isinstance(P.PaidTransport(ctx=None)._inner, T.Transport)


def test_only_paid_module_constructs_transport():
    """静态检查（AST，非字符串匹配）：付费路径只准经 PaidTransport。

    reviewer-r3 L1：字面匹配 ``T.Transport(`` 可被别名导入或
    ``getattr(T, "Transport")()`` 绕过；这里解析 AST 并追踪 import 来源，
    任何「解析到 transport.Transport 的构造调用」都算违规。扫描仍是启发式
    （动态 eval 不在覆盖面内），生产保证是「全仓没有第二处构造」。
    """
    import ast

    def _transport_aliases(tree: "ast.Module") -> "set[str]":
        names: "set[str]" = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("rsi_r01.transport"):
                    for a in node.names:
                        if a.name == "Transport":
                            names.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.endswith("rsi_r01.transport"):
                        names.add(a.asname or a.name)
        return names

    offenders = []
    for path, text in _source_scan(None).items():
        if path.name in ("transport.py", "paid.py"):
            continue
        tree = ast.parse(text, filename=str(path))
        aliases = _transport_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = False
            if isinstance(func, ast.Name) and func.id in aliases:
                hit = True
            elif isinstance(func, ast.Attribute) and func.attr == "Transport":
                hit = True
            elif (isinstance(func, ast.Name) and func.id == "getattr"
                  and len(node.args) >= 2
                  and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value == "Transport"):
                hit = True
            if hit:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"绕过 PaidTransport 直接构造 Transport：{offenders}"


def test_inner_cannot_be_reassigned():
    """L2：``_inner`` 是只读 property（无 setter），外部不能替换底层通道。"""
    tr = P.PaidTransport(ctx=None)
    with pytest.raises(AttributeError):
        tr._inner = T.Transport()
    with pytest.raises(AttributeError):
        tr.other = 1          # __slots__ 禁新增属性


def test_protocol_runner_defaults_to_paid_transport(monkeypatch):
    """T6 runner 必须默认持 PaidTransport（付费唯一入口）。"""
    from pathlib import Path
    from orchestrator.rsi_r01 import campaign as C
    from orchestrator.rsi_r01 import protocol as PR
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="paid_default_"))
    plan = tmp / "plan.md"; plan.write_text("p", encoding="utf-8")
    consent = tmp / "consent.txt"; consent.write_text("c", encoding="utf-8")
    ctx = C.create_campaign(campaign_id="paid_default", plan_path=plan,
                            consent_path=consent, repo_root=L.REPO_ROOT,
                            root=tmp / "campaigns")
    runner = PR.ProtocolRunner(ctx=ctx, model="m")
    assert isinstance(runner.transport, P.PaidTransport)
