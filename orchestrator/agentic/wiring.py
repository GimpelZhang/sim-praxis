"""组合根适配器（AC-T4-13）：RunSpec → agentic 内环 → 旧接缝记录形。

本件是图组合根（``orchestrator.graph.cli``）懒导入的唯一 agentic 入口：

- 鸭子类型读 RunSpec 的五个 agentic 字段（**不 import**
  agent_runner/graph——旧路径 import 面零 agentic/smolagents 由
  golden 子进程测试锁死）；
- :func:`run_agentic_for_runspec` 返回的 dict 与
  ``AgentRunner.run`` 的记录形同形（status/telemetry_uri/failure_json/
  coding_round/executions/timeline/reason），故 ``agent_run_seam``
  包装器与 ``nodes.py`` 一字不改；
- finish 不等于通过：status=ok 只代表内环自终局，evaluate/Gate2 照走
  （telemetry_uri 在 Task 5 真实 LIBERO 工厂/evaluate 接线前留空，
  trace 在 workdir 约定位置可发现，禁止伪造产物路径）。

授予串语法（Gate1 富卡 → RunSpec.tool_grants 的单真值解析，fail-closed）：

- 裸工具名（read_file/list_dir/grep/write_file/apply_patch/
  run_cmd/run_probe/recall/finish/fail）；
- ``run:a[:b:c]``：argv 白名单前缀，隐含授予 run_cmd；
- ``write:``（整个 workdir）/ ``write:<rel>``（相对前缀，禁绝对/``..``），
  隐含授予 write_file+apply_patch（NONPROD 下 policy 仍拒写，AC-T4-17）；
- ``probe:<name>``：白名单探针逐项授予，隐含 run_probe；
- ``netprobe:<name>``：探针 + 逐项网络授权（默认禁网）。
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Callable

from orchestrator.common.callcache import CallReplay
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text
from orchestrator.gateway.quota_ledger import (
    QuotaLedger,
    call_lines,
    current_billing,
    phase1_root,
    read_lines,
)

from .caps import BudgetTracker
from .config import ALL_OFF, AgenticConfig
from .forensics import TraceStore
from .loop import engine_for, run_agentic_inner_loop
from .policy import GrantTable

# 与 loop.py 注册名逐字一致（裸授予串白名单）。
# Phase 2.6 Task3：run_expr（F2，亦经 ``expr:`` 隐含授予）与
# update_scratchpad（F4，只经裸名授予）入表。计划 §T3.4-E 只写了
# 「TOOL_NAMES 加 run_expr（10 → 11）」，但 §T3.4-G 与 T3 的 Gate1 授予串
# 明写「需 Gate1 授予 update_scratchpad」，而裸名授予必须先在 TOOL_NAMES
# 内才不会被 ``AgenticWiringError`` 拒——故一并加入（12 项）。
TOOL_NAMES = frozenset({
    "read_file", "list_dir", "grep", "write_file", "apply_patch",
    "run_cmd", "run_probe", "recall", "finish", "fail",
    "run_expr", "update_scratchpad",
})
_WRITE_TOOLS = frozenset({"write_file", "apply_patch"})
# workdir/FAILURE.json：与 agent_runner.runner.FAILURE_JSON、
# graph/nodes.FAILURE_JSON 同字面量（不 import 旧引擎，保持依赖单向）。
FAILURE_JSON = "FAILURE.json"


class AgenticWiringError(ValueError):
    """出生契约装配失败（授予串非法等）：fail-closed，不进内环。"""


def build_seq_provider(
        *, ledger: "QuotaLedger | None" = None) -> "Callable[[], int | None]":
    """M5（2026-10-30 评审）：构造只读 seq 读取器。

    语义＝模型轮 int|null：实呼轮取该轮真实 ledger 整数 seq（含网关侧
    已记 ok=false 的 EmptyResponseError 行——seq 已被账套占用），缓存命中
    轮由 GatewayModel 显式传 null。读取面复用既有只读件，不新增 ledger
    接口、不改 router：``current_billing()`` 的 task/batch_id/thread_id
    身份 + 模块级 ``call_lines/read_lines`` 过滤本命名空间账整数 seq 行取
    max。无 billing 上下文时零 IO 直接返 None（hermetic 测试路径）。

    Phase 2.6（T0.4-G）：命名空间由构造参数显式给定，缺省随活跃账换期。
    Phase 2.7（Task0，锁内适配）：缺省 phase26 → **phase27**（phase26 已封存）。
    写死旧期会造成**静默证据腐化**——实呼已写进 phase27 账，而读取器仍盯
    封存的 phase26 账 ⇒ 恒返 None，而 trace 里 ``seq=null`` 的既定语义是
    「全缓存命中」，于是每一次真实付费调用都会被记成缓存命中。
    """
    holder: dict[str, QuotaLedger] = {}
    explicit = ledger

    def _read_seq() -> int | None:
        billing = current_billing()
        if billing is None:
            return None
        if "ledger" not in holder:
            holder["ledger"] = explicit or QuotaLedger.phase27()  # 只借命名空间解析账路径
        ledger = holder["ledger"]
        rows = call_lines(read_lines(ledger.ns.ledger_path_fn()))
        same_identity = [
            r for r in rows
            if r.get("task") == billing.task
            and r.get("batch_id") == billing.batch_id
            and (r.get("thread_id") or "") == (billing.thread_id or "")]
        if not same_identity:
            return None
        return max(int(r["seq"]) for r in same_identity)

    return _read_seq


def parse_grant_strings(grants: "tuple[str, ...]") -> GrantTable:
    """Gate1 授予串元组 → GrantTable；未知形态/偷渡路径立即 ValueError。"""
    tools: set[str] = set()
    run_argv: list[tuple[str, ...]] = []
    write_roots: list[str] = []
    probes: set[str] = set()
    network_probes: set[str] = set()
    expr_modules: set[str] = set()
    for raw in grants:
        if type(raw) is not str or not raw:
            raise AgenticWiringError(f"授予串必须是非空字符串：{raw!r}")
        if raw in TOOL_NAMES:
            tools.add(raw)
            continue
        if raw.startswith("run:"):
            argv = tuple(raw.split(":"))[1:]
            if not argv or any(not s for s in argv):
                raise AgenticWiringError(f"run 授予 argv 段不得为空：{raw!r}")
            tools.add("run_cmd")
            run_argv.append(argv)
        elif raw.startswith("write:"):
            rel = raw[len("write:"):]
            if rel:
                p = Path(rel)
                if p.is_absolute() or ".." in p.parts:
                    raise AgenticWiringError(
                        f"write 授予只允许 workdir 相对前缀：{raw!r}")
            tools |= _WRITE_TOOLS
            write_roots.append(rel)   # "" = 整个 workdir
        elif raw.startswith("probe:"):
            name = raw[len("probe:"):]
            if not name or ":" in name:
                raise AgenticWiringError(f"probe 授予名非法：{raw!r}")
            tools.add("run_probe")
            probes.add(name)
        elif raw.startswith("netprobe:"):
            name = raw[len("netprobe:"):]
            if not name or ":" in name:
                raise AgenticWiringError(f"netprobe 授予名非法：{raw!r}")
            tools.add("run_probe")
            probes.add(name)
            network_probes.add(name)
        elif raw.startswith("expr:"):
            # Phase 2.6 Task3（F2）：模块白名单（完整点分名，逗号分隔），
            # 隐含授予 run_expr。模块名只允许字母数字/下划线/点——空段、
            # 连字符、引号等一律 ValueError（fail-closed）。
            mods = tuple(raw[len("expr:"):].split(","))
            if not mods or any(
                    not m or m.startswith(".") or m.endswith(".")
                    or ".." in m
                    or not m.replace(".", "").replace("_", "").isalnum()
                    for m in mods):
                raise AgenticWiringError(f"expr 授予模块名非法：{raw!r}")
            tools.add("run_expr")
            expr_modules |= set(mods)
        else:
            raise AgenticWiringError(f"未知 Gate1 授予串：{raw!r}")
    return GrantTable(
        tools=frozenset(tools), run_argv=tuple(run_argv),
        write_roots=tuple(write_roots), probes=frozenset(probes),
        network_probes=frozenset(network_probes),
        expr_modules=frozenset(expr_modules))


def _build_task_text(spec: Any) -> str:
    """从出生契约既有字段拼内环任务文本（不加 RunSpec 第六字段）。"""
    parts = [str(getattr(spec, "task_title", "") or "").strip(),
             "出生/执行命令：" + shlex.join(str(c) for c in spec.cmd)]
    notes = getattr(spec, "gate2_notes", None)
    if notes:
        parts.append("Gate2 打回意见：\n" + str(notes))
    return "\n\n".join(p for p in parts if p)


def _open_trace(spec: Any) -> TraceStore:
    """trace 目录：显式 relpath 优先（thread_id 槽位传完整相对目录），

    否则约定 workdir/agentic_traces/<thread_id>。两种形态落盘位置一致
    （root/thread_id/trace.jsonl），relpath 的 '..'/绝对已在 RunSpec
    __post_init__ 拒掉。
    """
    rel = getattr(spec, "agentic_trace_relpath", None)
    if rel:
        return TraceStore(Path(spec.workdir), rel)
    return TraceStore(Path(spec.workdir) / "agentic_traces", spec.thread_id)


def run_agentic_for_runspec(
    spec: Any, *, gateway: Any,
    recall_port: Any = None,
    run_backend: Any = None,
    probe_registry: Any = None,
    call_cache: Any = None,
    nonprod: bool = False,
) -> dict[str, Any]:
    """按 agentic RunSpec 跑一次内环，返回旧接缝同形记录（0 裸异常）。"""
    if engine_for(spec) != "agentic":
        # 组合根分派器的防御性兜底（正常路径懒加载前已按属性分流）。
        raise AgenticWiringError(
            "run_agentic_for_runspec 只能处理 agentic=True 的 RunSpec")
    grants = parse_grant_strings(tuple(spec.tool_grants))
    # Phase 2.6 Task3：AgenticConfig 缺省 None = 全关 = Phase 2.5 旧行为。
    cfg = getattr(spec, "agentic_config", None)
    if cfg is not None and not isinstance(cfg, AgenticConfig):
        raise AgenticWiringError(
            f"RunSpec.agentic_config 必须是 AgenticConfig|None，收到 "
            f"{type(cfg).__name__}")
    if cfg is None:
        cfg = ALL_OFF
    budget = BudgetTracker(
        step_cap=spec.step_cap, token_cap=spec.token_cap,
        round_cap=max(int(spec.max_coding_rounds), 1),
        verify_repair_tail_rounds=(cfg.verify_repair_tail_rounds
                                   if cfg.verify_repair_tail else 0))
    tracer = _open_trace(spec)
    if call_cache is None:
        # 与 AgentRunner 的默认 CallReplay 同一目录（runner.py 落点见
        # test_task0_debt 白名单）：agentic-v2-/v3-/v4- 三纪元键空间
        # 与 coding_ 键空间零交集（前缀分隔），历史 coding_/macro_ 键不动。
        # （Phase 2.7 Task3 D3-7 新增 v4：env_spec/render_preview 开启后的
        # 键空间天然为空，不会命中不含 ENV_SPEC 的旧 v2/v3 响应。）
        call_cache = CallReplay(phase1_root() / ".call_cache")
    outcome = run_agentic_inner_loop(
        workdir=spec.workdir, task=_build_task_text(spec),
        grants=grants, gateway=gateway, budget=budget, tracer=tracer,
        thread_id=spec.thread_id, run_backend=run_backend,
        probe_registry=probe_registry, recall_port=recall_port,
        call_cache=call_cache, nonprod=nonprod,
        # M5：只读实呼 seq 读取器（无 billing/全缓存轮 trace seq=null）。
        seq_provider=build_seq_provider(),
        agentic_config=cfg)

    common = {
        "coding_round": outcome.rounds_used,
        "executions": outcome.steps_used,
        "timeline": [],          # agentic 逐步证据在 trace.jsonl，不进图 state
    }
    if not outcome.is_failure_artifact:
        # finish/implicit_final_answer：自终局≠通过，evaluate/Gate2 独立判定。
        # telemetry 只认真实落盘的出生契约产物（spec.telemetry_relpath）：
        # 不存在绝不伪造路径——node_agent_run 会以 GraphStateError 诚实拦下，
        # Task 5 真实 agentic 工厂负责保证产物契约。
        telemetry = Path(spec.workdir) / str(spec.telemetry_relpath)
        telemetry_uri = str(telemetry) if telemetry.is_file() else ""
        return {"status": "ok", "telemetry_uri": telemetry_uri,
                "video_path": None, "duration_ms": None,
                "cover_png": None, "intranet_url": None, **common}

    # 到帽/自报失败/供应侧/引擎错：优雅 FAILURE 件（workdir 级，
    # nodes 的图级 FAILURE 交叉引用逻辑无需改动即可读取）。
    reason = f"[agentic:{outcome.kind}] {outcome.reason}".strip()
    payload = {
        "status": "failed",
        "engine": "agentic",
        "thread_id": spec.thread_id,
        "agentic_kind": outcome.kind,
        "reason": reason,
        "coding_round": outcome.rounds_used,
        "executions": outcome.steps_used,
        "tokens_used": outcome.tokens_used,
        "cache_hits": outcome.cache_hits,
        "empty_responses": outcome.empty_responses,
        "supplier_errors": outcome.supplier_errors,
        "agentic_trace": str(tracer.trace_path),
        "ts_iso": utc_now_iso_z(),
    }
    p = Path(spec.workdir) / FAILURE_JSON
    atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
    return {"status": "failed", "failure_json": str(p),
            "reason": reason, **common}
