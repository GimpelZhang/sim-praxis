#!/usr/bin/env python3
"""Phase 2.5 Task3 Stage0 — P1 纯文本工具协议传输探针（AC-T3-2，恰好 2 POST/2 seq）。

**一次性探针，不是生产路径**：LangGraph 图/runner/smolagents 产品件永不导入
本脚本（调用点由 tests/milestone25/test_task3_probes.py 源码红线扫描锁定，
仅本文件；本脚本也永不 import orchestrator.graph/orchestrator.agentic）。
P1 在不使用原生 tool_use 的前提下，经现有 /api/coding Anthropic-Messages shim
调 coding_debug 现模型（stage 名解析，业务码零 model_id/供应商 URL，绝不
image block），判三件事实（判据预注册，禁止见结果调界）：

  1. POST1：模型能否在**单个响应**内以一个 JSON **数组并行**发起两只只读
     工具调用（list_dir + read_file）——纯文本围栏协议，JSON 可解析率
     必须 100%（n=1，含数组并行形态；现模型有 Task8 格式劣化史，见 Task8 结案）；
  2. POST2：两个工具结果按序回注后，模型能否续作产出最终答案；
  3. stop：POST2 不得再发工具调用，且必须以哨兵 <<<DONE>>> 收尾。

模型在 POST1 即提前给出最终答案时，POST2 自动切到「追加一次观察回注恢复」
形态（beta.txt 新观察），判据不省（恢复后答案须纳入 beta 标记 + 哨兵）。

安全/计费纪律（与既有 phase2 一次性付费诊断探针同款纪律）：
  - 显式付费守卫 SIMPRAXIS_ALLOW_PAID=1，裸跑零触网 exit 2；paid pytest
    marker 同变量双守卫，裸跑 skip；
  - ModelGateway(ledger=QuotaLedger.phase25(), max_attempts=1)：phase25
    独立新账（T0 D-1），单发——供应侧任何出口原样留存占 seq，不重试到
    成功（P6：超时不豁免）；billing_context(task=3, batch_id=P25-B1)；
  - 仅 coding_debug 文本 stage，零 image block，不接受模型覆盖参数；
  - req/resp 原文逐件 0600 落 $SIMPRAXIS_PHASE25_ROOT/probes/stage0/
    （认证只在 header，正文不含凭据；目录 0700，证据永不入库/不入模型正文）。

退出码：0=P1 两 POST 全过；2=守卫/参数错（零触网）；10=供应侧明确拒绝
（NonRetryableError，证据已落）；1=其他停止出口（连接/熔断/缺票，原因看
phase25 ledger 该行 err）；3=P1 判负（协议事实不成立，证据完整）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_text,
    ensure_private_dir,
)
from orchestrator.common.runtime_paths import phase25_ledger_path, phase25_root  # noqa: E402
from orchestrator.common.validators import validate_batch_id, validate_thread_id  # noqa: E402
from orchestrator.gateway.quota_ledger import (  # noqa: E402
    QuotaLedger,
    billing_context,
    read_lines,
)
from orchestrator.gateway.router import ModelGateway, NonRetryableError  # noqa: E402

STAGE = "coding_debug"           # 硬规则 1：只走文本 coding stage
TASK = 3
MAX_ATTEMPTS = 1                 # 单发：供应侧异常原样留存，不重试到成功
MAX_TOKENS = 4096
TEMPERATURE = 0
TIMEOUT_S = 300
STOP_SENTINEL = "<<<DONE>>>"
TOOL_NAMES = ("list_dir", "read_file")
ALPHA_MARKER = "alpha-marker-7f3a"
BETA_MARKER = "beta-marker-9c1e"

EXIT_GUARD = 2
EXIT_REJECTED = 10
EXIT_ERROR = 1
EXIT_FAIL = 3

_SYSTEM_PROTOCOL = """你是工具协议传输测试中的受控模型。本测试不写产品代码。
可用工具（只读，均在工作目录内）：
- list_dir(path): 列出目录条目；
- read_file(path): 读取 UTF-8 文本文件。
工具调用协议（严格遵守）：
- 需要调用工具时，只输出一个 JSON 数组，数组每个元素是一次工具调用，
  形如 {"tool": "read_file", "arguments": {"path": "alpha.txt"}}；
- 数组可以包含多个元素，表示同一响应内的并行调用；
- 除 JSON 数组外不得输出任何解释或 markdown 正文；允许 ```json 围栏。
收到工具结果后：根据结果给出最终答案，答案末尾必须另起一行输出哨兵
""" + STOP_SENTINEL + """；收到结果后禁止再次输出工具调用 JSON。"""


# ---------------------------------------------------------------------------
# 纯解析/判读逻辑（hermetic 单测全覆盖；mock 网关，0 付费 0 触网）
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_array(text: str) -> dict:
    """从模型响应中抽取唯一一个 JSON 数组。

    返回 {parsed: list|None, candidates: int, shape_ok: bool}。
    candidates=成功解析为 list 的候选数；判据要求恰好 1 个（100% 可解析率，
    n=1 响应）。解析顺序：整体直解 → 围栏块 → 首 '[' 到末 ']' 截取。
    """
    raw = text or ""
    candidates: list[list] = []

    def _try(s: str) -> None:
        try:
            v = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(v, list):
            candidates.append(v)

    _try(raw.strip())
    for m in _FENCE_RE.finditer(raw):
        _try(m.group(1).strip())
    if not candidates:
        i, j = raw.find("["), raw.rfind("]")
        if 0 <= i < j:
            _try(raw[i:j + 1])
    parsed = candidates[0] if len(candidates) == 1 else None
    return {"parsed": parsed, "candidates": len(candidates),
            "shape_ok": len(candidates) == 1}


def judge_post1(text: str) -> dict:
    """POST1 判读：恰好 1 个可解析数组、2 个元素、并行覆盖两只工具、参数合法。"""
    ext = extract_json_array(text)
    reasons: list[str] = []
    calls = ext["parsed"]
    if not ext["shape_ok"]:
        reasons.append(f"json_array_candidates={ext['candidates']}（须恰好 1）")
        calls = None
    names: list[str] = []
    args_ok = True
    if isinstance(calls, list):
        if len(calls) != 2:
            reasons.append(f"array_len={len(calls)}（须恰好 2，并行形态）")
        for idx, c in enumerate(calls):
            if not isinstance(c, dict):
                reasons.append(f"call[{idx}] 不是对象")
                args_ok = False
                continue
            name = c.get("tool") or c.get("name")
            args = c.get("arguments") or c.get("args") or {}
            if not isinstance(name, str):
                reasons.append(f"call[{idx}] 缺 tool 名")
                args_ok = False
                continue
            names.append(name)
            if not isinstance(args, dict) or not isinstance(args.get("path"), str):
                reasons.append(f"call[{idx}]({name}) arguments.path 非法")
                args_ok = False
        if sorted(names) != sorted(TOOL_NAMES):
            reasons.append(f"工具集={sorted(names)}（须恰为 {sorted(TOOL_NAMES)}）")
    return {"pass": ext["shape_ok"] and not reasons and args_ok,
            "reasons": reasons, "calls": calls,
            "json_parse_rate": 1.0 if ext["shape_ok"] else 0.0}


def looks_finished(text: str) -> bool:
    """POST1 提前完成探测：已带哨兵（此时 POST2 切追加观察恢复形态）。"""
    return STOP_SENTINEL in (text or "")


def judge_post2(text: str, *, required_markers: "list[str]",
                extra_array_allowed: bool = False) -> dict:
    """POST2 判读：续作 + 引用观察标记 + 哨兵收尾 + 不再发工具调用。"""
    reasons: list[str] = []
    tail = (text or "").rstrip()
    if not tail.endswith(STOP_SENTINEL):
        reasons.append("响应未以哨兵收尾（stop 未被尊重）")
    for mk in required_markers:
        if mk not in (text or ""):
            reasons.append(f"答案未纳入观察标记 {mk}（观察回注恢复失败）")
    ext = extract_json_array(text)
    if ext["shape_ok"] and not extra_array_allowed:
        reasons.append("回注后又输出工具调用 JSON（stop 未被尊重）")
    return {"pass": not reasons, "reasons": reasons,
            "json_array_candidates": ext["candidates"]}


# ---------------------------------------------------------------------------
# 我方只读工具执行（工具在我方；模型只产出调用文本）
# ---------------------------------------------------------------------------

def make_workspace(root: Path) -> dict:
    """建两个只读探针夹具文件，返回 {path: content} 事实表。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    alpha = f"alpha report line1\nmarker {ALPHA_MARKER}\n"
    beta = f"beta supplemental\nmarker {BETA_MARKER}\n"
    (root / "alpha.txt").write_text(alpha, encoding="utf-8")
    (root / "beta.txt").write_text(beta, encoding="utf-8")
    return {"alpha.txt": alpha, "beta.txt": beta}


def exec_readonly_tool(name: str, args: dict, workspace: Path) -> str:
    """在 workspace jail 内执行一只只读工具（拒绝越界）。"""
    workspace = Path(workspace).resolve()
    p = (workspace / args["path"]).resolve()
    if workspace not in (p, *p.parents) and p != workspace:
        raise ValueError("path escapes workspace")
    if name == "list_dir":
        return "\n".join(sorted(x.name for x in p.iterdir()))
    if name == "read_file":
        return p.read_text(encoding="utf-8")
    raise ValueError(f"unknown tool {name}")


POST1_USER = ("第一步：在同一个响应内【并行】调用两只工具——list_dir(path='.') "
              "与 read_file(path='alpha.txt')，用一个含两个元素的 JSON 数组表达。"
              "本响应只允许输出该 JSON 数组。")


def build_post1_messages() -> list[dict]:
    return [{"role": "system", "content": [{"type": "text", "text": _SYSTEM_PROTOCOL}]},
            {"role": "user", "content": [{"type": "text", "text": POST1_USER}]}]


def build_post2_messages(post1_text: str, observations: "list[tuple[str,str,str]]",
                         *, beta_recovery: bool) -> list[dict]:
    """observations: [(tool_name, path, result), ...]，按 POST1 数组顺序回注。"""
    blocks = ["工具结果已回注（按调用顺序）："]
    for i, (name, path, result) in enumerate(observations):
        blocks.append(
            f"<tool_result index={i} tool={name} path={path}>\n{result}\n</tool_result>")
    if beta_recovery:
        blocks.append(
            "追加观察：目录中新增了 beta.txt（一次额外的目录观察已发生），"
            f"其 read_file 结果为：\n<tool_result tool=read_file path=beta.txt>\n"
            f"{make_workspace_beta()}</tool_result>")
        blocks.append(
            "请恢复续作：把 alpha 与 beta 两份观察的标记都纳入最终答案，"
            f"末尾另起一行输出 {STOP_SENTINEL}，禁止再输出工具调用 JSON。")
    else:
        blocks.append(
            "请续作：给出最终答案（须引用 alpha 观察中的标记），"
            f"末尾另起一行输出 {STOP_SENTINEL}，禁止再输出工具调用 JSON。")
    return [
        {"role": "system", "content": [{"type": "text", "text": _SYSTEM_PROTOCOL}]},
        {"role": "user", "content": [{"type": "text", "text": POST1_USER}]},
        {"role": "assistant", "content": [{"type": "text", "text": post1_text}]},
        {"role": "user", "content": [{"type": "text", "text": "\n".join(blocks)}]},
    ]


def make_workspace_beta() -> str:
    return f"beta supplemental\nmarker {BETA_MARKER}\n"


# ---------------------------------------------------------------------------
# 证据与账本
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_evidence(path: Path, payload: dict) -> None:
    ensure_private_dir(path.parent)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", mode=0o600)


def last_thread_seq(thread_id: str) -> "int | None":
    """从 phase25 账取本线程最后一个整数 seq（FakeGateway 测试路径无账→None）。"""
    try:
        rows = [r for r in read_lines(phase25_ledger_path())
                if r.get("thread_id") == thread_id and isinstance(r.get("seq"), int)]
    except (OSError, ValueError):
        return None
    return rows[-1]["seq"] if rows else None


# ---------------------------------------------------------------------------
# 协议执行（gateway 可注入：付费走真 ModelGateway，单测走 FakeGateway）
# ---------------------------------------------------------------------------

def run_protocol(gateway, *, out_dir: Path, batch_id: str, thread_id: str,
                 workspace: Path) -> dict:
    out_dir = Path(out_dir)
    facts = make_workspace(workspace)
    verdict: dict = {"probe": "p1_plaintext_tools", "batch_id": batch_id,
                     "thread_id": thread_id, "captured_at_iso": _utcnow(),
                     "stage": STAGE, "posts": {}}

    # ---- POST1 ----
    m1 = build_post1_messages()
    p1: dict = {"post": 1, "request": {"messages": m1, "max_tokens": MAX_TOKENS,
                                        "temperature": TEMPERATURE, "timeout_s": TIMEOUT_S,
                                        "stage": STAGE}}
    try:
        with billing_context(task=TASK, batch_id=batch_id, thread_id=thread_id):
            text1, usage1 = gateway.call(STAGE, m1, max_tokens=MAX_TOKENS,
                                         temperature=TEMPERATURE, timeout=TIMEOUT_S)
    except NonRetryableError as exc:
        p1.update(outcome="supplier_rejected", error=type(exc).__name__,
                  error_detail=str(exc)[:500])
        write_evidence(out_dir / "p1_post1_resp.json", p1)
        verdict["posts"]["1"] = {"outcome": "supplier_rejected"}
        write_evidence(out_dir / "p1_verdict.json", verdict)
        return {"rc": EXIT_REJECTED, "verdict": verdict}
    except Exception as exc:  # noqa: BLE001 —— 越过 preflight 的任何出口：账已记一行
        p1.update(outcome="error", error=type(exc).__name__,
                  error_detail=str(exc)[:500])
        write_evidence(out_dir / "p1_post1_resp.json", p1)
        verdict["posts"]["1"] = {"outcome": "error"}
        write_evidence(out_dir / "p1_verdict.json", verdict)
        return {"rc": EXIT_ERROR, "verdict": verdict}

    j1 = judge_post1(text1)
    p1.update(outcome="ok", response_text=text1,
              usage={"input_tokens": usage1[0], "output_tokens": usage1[1]},
              judgment=j1, ledger_seq=last_thread_seq(thread_id))
    write_evidence(out_dir / "p1_post1_req.json",
                   {"post": 1, "captured_at_iso": _utcnow(), **p1["request"]})
    write_evidence(out_dir / "p1_post1_resp.json", p1)
    verdict["posts"]["1"] = {"outcome": "ok", "judgment_pass": j1["pass"],
                             "seq": p1["ledger_seq"]}
    if not j1["pass"]:
        # POST1 判负：无法构造合法观察回注，不补呼（P1 恰负 1 seq 也比伪续作诚实）。
        verdict["p1_pass"] = False
        verdict["stop_reason"] = "post1_protocol_failed"
        write_evidence(out_dir / "p1_verdict.json", verdict)
        return {"rc": EXIT_FAIL, "verdict": verdict}

    # ---- POST2（POST1 提前完成 → 追加观察恢复形态） ----
    obs = []
    for c in j1["calls"]:
        name = c.get("tool") or c.get("name")
        args = c.get("arguments") or c.get("args")
        obs.append((name, args["path"], exec_readonly_tool(name, args, workspace)))
    beta = looks_finished(text1)
    m2 = build_post2_messages(text1, obs, beta_recovery=beta)
    markers = [ALPHA_MARKER] + ([BETA_MARKER] if beta else [])
    p2: dict = {"post": 2, "mode": "extra_observation_recovery" if beta else "continue",
                "request": {"messages": m2, "max_tokens": MAX_TOKENS,
                            "temperature": TEMPERATURE, "timeout_s": TIMEOUT_S,
                            "stage": STAGE}}
    try:
        with billing_context(task=TASK, batch_id=batch_id, thread_id=thread_id):
            text2, usage2 = gateway.call(STAGE, m2, max_tokens=MAX_TOKENS,
                                         temperature=TEMPERATURE, timeout=TIMEOUT_S)
    except NonRetryableError as exc:
        p2.update(outcome="supplier_rejected", error=type(exc).__name__,
                  error_detail=str(exc)[:500])
        write_evidence(out_dir / "p1_post2_resp.json", p2)
        verdict["posts"]["2"] = {"outcome": "supplier_rejected"}
        write_evidence(out_dir / "p1_verdict.json", verdict)
        return {"rc": EXIT_REJECTED, "verdict": verdict}
    except Exception as exc:  # noqa: BLE001
        p2.update(outcome="error", error=type(exc).__name__,
                  error_detail=str(exc)[:500])
        write_evidence(out_dir / "p1_post2_resp.json", p2)
        verdict["posts"]["2"] = {"outcome": "error"}
        write_evidence(out_dir / "p1_verdict.json", verdict)
        return {"rc": EXIT_ERROR, "verdict": verdict}

    j2 = judge_post2(text2, required_markers=markers,
                     extra_array_allowed=False)
    p2.update(outcome="ok", response_text=text2,
              usage={"input_tokens": usage2[0], "output_tokens": usage2[1]},
              judgment=j2, ledger_seq=last_thread_seq(thread_id))
    write_evidence(out_dir / "p1_post2_req.json",
                   {"post": 2, "captured_at_iso": _utcnow(), **p2["request"]})
    write_evidence(out_dir / "p1_post2_resp.json", p2)
    verdict["posts"]["2"] = {"outcome": "ok", "judgment_pass": j2["pass"],
                             "mode": p2["mode"], "seq": p2["ledger_seq"]}
    verdict["p1_pass"] = bool(j1["pass"] and j2["pass"])
    verdict["stop_reason"] = "completed"
    write_evidence(out_dir / "p1_verdict.json", verdict)
    return {"rc": 0 if verdict["p1_pass"] else EXIT_FAIL, "verdict": verdict}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_p1_plaintext_tools.py",
        description="P1 纯文本工具协议传输探针（恰好 2 POST，显式付费守卫）")
    p.add_argument("--batch-id", required=True, help="配额票号（P25-B1）")
    p.add_argument("--thread", default="p25t3-p1", help="记账线程")
    p.add_argument("--out-dir", default=None,
                   help="证据目录（默认 $SIMPRAXIS_PHASE25_ROOT/probes/stage0）")
    p.add_argument("--workspace", default=None,
                   help="只读工具夹具目录（默认证据目录/p1_workspace）")
    return p


def main(argv: "list[str] | None" = None) -> int:
    # 首行付费守卫：裸跑零触网（与 paid pytest 同一环境变量）。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("probe_p1: 拒绝运行——本探针发起 2 次真实付费 POST，必须显式设置 "
              "SIMPRAXIS_ALLOW_PAID=1（裸跑守卫，零触网 exit 2）", file=sys.stderr)
        return EXIT_GUARD
    args = build_parser().parse_args(argv)
    try:
        validate_batch_id(args.batch_id)
        validate_thread_id(args.thread)
    except ValueError as exc:
        print(f"probe_p1: 参数非法：{exc}", file=sys.stderr)
        return EXIT_GUARD
    out_dir = Path(args.out_dir) if args.out_dir else \
        phase25_root() / "probes" / "stage0"
    workspace = Path(args.workspace) if args.workspace else out_dir / "p1_workspace"
    gateway = ModelGateway(ledger=QuotaLedger.phase25(), max_attempts=MAX_ATTEMPTS)
    result = run_protocol(gateway, out_dir=out_dir, batch_id=args.batch_id,
                          thread_id=args.thread, workspace=workspace)
    rc = result["rc"]
    v = result["verdict"]
    if rc == 0:
        print(f"probe_p1: PASS 两 POST 全过，证据→{out_dir}")
    else:
        print(f"probe_p1: 停止 rc={rc} posts={list(v['posts'])} 证据→{out_dir}",
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
