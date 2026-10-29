#!/usr/bin/env python3
"""Phase 2.5 Task3 Stage0 — P2 Anthropic 原生 tool_use 传输探针（AC-T3-3，≤3 POST）。

**一次性探针，不是生产路径**（红线同 probe_p1：本文件是唯一调用点，
永不 import orchestrator.graph/orchestrator.agentic）。P2 经同一个
/api/coding shim + coding_debug 现模型（stage 名解析，零 model_id/供应商
URL，零 image block）验证 Anthropic 原生 tools：

  - POST1 单工具往返；
  - POST2 单响应双工具并行：两个 tool_use id 不串、结果按 id 各自回灌合法
    （回灌报文在本地构造并校验 1:1 映射，不另发 POST——每形态恰 1 seq）；
  - POST3 截断到大限附近的长 tool_result 观察后恢复续作（尾部标记取证）。

任一形态不支持即该 POST 判 fail 并原样留存供应侧报文；提前失败即停，
不补呼。供应侧任何出口占 seq、不重试到成功（max_attempts=1）。

**锁内适配（传输缝，2026-10-29）**：ModelGateway.call() 的固定签名不透传
``tools``（Text 阶段契约，AC-T4-1 规定 CodeAgent 路径也永不传 tools）。
本探针不改任何产品代码，改用构造器**既有文档化注入点**
``transport=``（router.py 注释：「测试注入点：transport=litellm.completion
替身」）挂一个【富化包装器】：网关仍按 stage 名解析模型、做 phase25
preflight/记账/单发；包装器仅在发往 litellm.completion 的 kwargs 上追加
tools= 并原样捕获响应对象（model_dump 快照）。业务码仍零 model_id/URL/
凭据（api_key 从 kwargs 剔除后才落证）。若模型只回 tool_use 无文本，
gateway 按 G-12 文本空契约记 ok=false 并抛 EmptyResponseError——此时科学
判决以捕获到的原始响应为准（tool_use 形态成立与否），ledger ok 标志在
证据与对账表中如实分列，不改写、不掩饰。

纪律：SIMPRAXIS_ALLOW_PAID=1 首行守卫（裸跑 exit 2 零触网）；paid marker；
ModelGateway(ledger=QuotaLedger.phase25(), max_attempts=1)；
billing_context(task=3, batch_id=P25-B1)；req/resp 原文 0600 落
$SIMPRAXIS_PHASE25_ROOT/probes/stage0/。

退出码：0=三形态全过；2=守卫/参数错；10=供应侧明确拒绝（形态不支持，
如 400）；1=其他停止出口；3=形态判负（提前失败即停）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import phase25_ledger_path, phase25_root  # noqa: E402
from orchestrator.common.validators import validate_batch_id, validate_thread_id  # noqa: E402
from orchestrator.gateway.quota_ledger import (  # noqa: E402
    QuotaLedger,
    billing_context,
    read_lines,
)
from orchestrator.gateway.router import ModelGateway, NonRetryableError  # noqa: E402

STAGE = "coding_debug"
TASK = 3
MAX_ATTEMPTS = 1
MAX_TOKENS = 4096
TEMPERATURE = 0
TIMEOUT_S = 300
HEAD_MARKER = "HEAD-MARKER-3K9Q"
TAIL_MARKER = "TAIL-MARKER-8Z2W"
LONG_OBS_CHARS = 60000

EXIT_GUARD = 2
EXIT_REJECTED = 10
EXIT_ERROR = 1
EXIT_FAIL = 3

TOOLS_SPEC = [
    {"type": "function",
     "function": {"name": "list_dir",
                  "description": "列出一个目录内的文件与子目录条目（只读）。",
                  "parameters": {"type": "object",
                                 "properties": {"path": {"type": "string"}},
                                 "required": ["path"]}}},
    {"type": "function",
     "function": {"name": "read_file",
                  "description": "读取工作目录内一个 UTF-8 文本文件（只读）。",
                  "parameters": {"type": "object",
                                 "properties": {"path": {"type": "string"}},
                                 "required": ["path"]}}},
]


# ---------------------------------------------------------------------------
# litellm 传输：tools 富化包装器 + 响应快照（_call_completion 为测试替身点）
# ---------------------------------------------------------------------------

def _call_completion(**kwargs: Any) -> Any:
    """生产路径的唯一 litellm 触网点（惰性 import，测试 monkeypatch 本符号）。"""
    import litellm  # noqa: PLC0415
    return litellm.completion(**kwargs)


def _snapshot(resp: Any) -> dict:
    """把 litellm ModelResponse 归一成纯 dict 快照（证据 + 判读同源）。"""
    if hasattr(resp, "model_dump"):
        try:
            return json.loads(json.dumps(resp.model_dump(), default=str))
        except Exception:  # noqa: BLE001 —— 快照失败退 getattr 形态，不丢证据
            pass
    msg = getattr(getattr(resp, "choices", [None])[0], "message", None)
    return {"choices": [{"message": getattr(msg, "__dict__", {}),
                         "finish_reason": getattr(getattr(resp, "choices", [None])[0],
                                                  "finish_reason", None)}],
            "usage": getattr(getattr(resp, "usage", None), "__dict__", {})}


def make_tools_transport(capture: dict, *, tools: "list[dict] | None" = None):
    """挂到 ModelGateway(transport=) 的富化包装器。

    网关传入的 kwargs 原样转发，仅：(1) 注入 tools（不注入 tool_choice——
    auto 才能如实观测模型自觉形态）；(2) 响应对象原样返回前落快照。
    """
    tools = TOOLS_SPEC if tools is None else tools

    def _transport(**kwargs: Any) -> Any:
        kwargs = dict(kwargs)
        kwargs["tools"] = tools
        resp = _call_completion(**kwargs)
        capture["req_kwargs"] = {k: v for k, v in kwargs.items()
                                 if k not in ("api_key",)}
        capture["raw"] = _snapshot(resp)
        return resp

    return _transport


# ---------------------------------------------------------------------------
# 纯判读逻辑（hermetic 单测：并行 id 串扰/回灌合法性/长观察恢复）
# ---------------------------------------------------------------------------

def _first_choice(raw: dict) -> dict:
    try:
        return raw["choices"][0] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def parse_message_tool_calls(raw: dict) -> list[dict]:
    """从响应快照抽取标准化 tool_calls：[{id,type,name,arguments(dict|str)}]。"""
    msg = _first_choice(raw).get("message") or {}
    out: list[dict] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass
        out.append({"id": tc.get("id"), "type": tc.get("type"),
                    "name": fn.get("name"), "arguments": args})
    return out


def message_text(raw: dict) -> str:
    content = (_first_choice(raw).get("message") or {}).get("content")
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return content or ""


def judge_single(raw: dict) -> dict:
    calls = parse_message_tool_calls(raw)
    reasons = []
    if len(calls) != 1:
        reasons.append(f"tool_calls 数={len(calls)}（须恰好 1）")
    if calls and calls[0]["name"] != "list_dir":
        reasons.append(f"工具名={calls[0]['name']}（须 list_dir）")
    if calls and not isinstance(calls[0]["arguments"], dict):
        reasons.append("arguments 非 JSON 对象")
    elif calls and not isinstance(calls[0]["arguments"].get("path"), str):
        reasons.append("arguments.path 非字符串")
    return {"pass": not reasons, "reasons": reasons, "calls": calls}


def judge_parallel(raw: dict) -> dict:
    calls = parse_message_tool_calls(raw)
    reasons = []
    if len(calls) != 2:
        reasons.append(f"tool_calls 数={len(calls)}（须恰好 2，单响应并行）")
    ids = [c["id"] for c in calls]
    if len(set(ids)) != len(ids):
        reasons.append(f"tool_use id 串扰/重复：{ids}")
    if not all(ids):
        reasons.append(f"存在空 id：{ids}")
    names = sorted(c["name"] for c in calls)
    if names != sorted(("list_dir", "read_file")):
        reasons.append(f"工具集={names}（须 [list_dir, read_file]）")
    for c in calls:
        if not isinstance(c["arguments"], dict) or \
                not isinstance(c["arguments"].get("path"), str):
            reasons.append(f"{c['name']} arguments.path 非法")
    # 回灌合法性：构造 1:1 映射（同批结果互相错放即抛）。
    feedback = None
    if not reasons:
        results = {c["id"]: (c["name"], f"OBS[{c['name']}:{c['arguments']['path']}]")
                   for c in calls}
        try:
            feedback = build_tool_feedback(calls, results)
        except ValueError as exc:
            reasons.append(f"结果回灌不合法：{exc}")
    return {"pass": not reasons, "reasons": reasons, "calls": calls,
            "feedback_messages": feedback}


def build_tool_feedback(calls: "list[dict]",
                        results: "dict[str, tuple[str, str]]") -> list[dict]:
    """按 tool_use id 把工具结果各自回灌（OpenAI/litellm 形态，本地构造零 POST）。

    results: {tool_use_id: (tool_name, observation_text)}。每个 id 必须恰好
    使用一次且与 calls 中同 id 的工具名一致——错放/串 id/多回少回都 fail。
    """
    ids = [c["id"] for c in calls]
    if set(results) != set(ids):
        raise ValueError(
            f"结果 id 集 {sorted(map(str, results))} 与 tool_use id 集 {ids} 不一致")
    assistant_tcs = [
        {"id": c["id"], "type": "function",
         "function": {"name": c["name"],
                      "arguments": json.dumps(c["arguments"], ensure_ascii=False)
                      if isinstance(c["arguments"], dict) else c["arguments"]}}
        for c in calls]
    messages = [{"role": "assistant", "content": None, "tool_calls": assistant_tcs}]
    for c in calls:  # 按响应顺序逐 id 回灌（同名字段交叉核对，串号即 ValueError）
        name, obs = results[c["id"]]
        if name != c["name"]:
            raise ValueError(f"id={c['id']} 结果工具名 {name} 与调用名 {c['name']} 不符")
        messages.append({"role": "tool", "tool_call_id": c["id"],
                         "name": name, "content": obs})
    return messages


def long_observation(chars: int = LONG_OBS_CHARS) -> str:
    """大限附近的长观察：首行头标记、末行尾标记，中间确定性填充行。"""
    lines = [f"[line 0] {HEAD_MARKER}"]
    i = 1
    body = ""
    while len(body) < chars - 200:
        body += f"[line {i}] record-{i:05d}|data-" + ("x" * 40) + "\n"
        i += 1
    return lines[0] + "\n" + body + f"[line {i}] {TAIL_MARKER}\n"


def judge_long_recovery(raw: dict) -> dict:
    text = message_text(raw)
    reasons = []
    if HEAD_MARKER not in text:
        reasons.append("响应未复原头标记（长观察前段丢失）")
    if TAIL_MARKER not in text:
        reasons.append("响应未复原尾标记（截断附近恢复失败）")
    return {"pass": not reasons, "reasons": reasons,
            "text_chars": len(text)}


# ---------------------------------------------------------------------------
# 形态请求构造
# ---------------------------------------------------------------------------

def form1_messages() -> list[dict]:
    return [{"role": "user", "content":
             "请调用 list_dir 工具列出当前目录（path 传 '.'）。只调用这一个工具。"}]


def form2_messages() -> list[dict]:
    return [{"role": "user", "content":
             "请在【同一个响应】里并行调用两个工具：list_dir(path='.') 与 "
             "read_file(path='alpha.txt')。两个 tool_use 必须在同一响应中，"
             "不要分两次回答。"}]


def form3_messages() -> list[dict]:
    obs = long_observation()
    return [
        {"role": "user", "content": "请用 read_file 读取报告 report.txt。"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "toolu_p23_seed", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": json.dumps({"path": "report.txt"})}}]},
        {"role": "tool", "tool_call_id": "toolu_p23_seed", "name": "read_file",
         "content": obs},
        {"role": "user", "content":
         f"这份工具观察很长（约 {len(obs)} 字符）。观察中有两个固定标记，"
         "一个在开头附近、一个在结尾附近。请原样给出这两个标记（各出现一次，"
         "逐字引用），然后结束。"},
    ]


# ---------------------------------------------------------------------------
# 证据/账/执行
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_evidence(path: Path, payload: dict) -> None:
    ensure_private_dir(path.parent)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", mode=0o600)


def last_thread_seq(thread_id: str) -> "int | None":
    try:
        rows = [r for r in read_lines(phase25_ledger_path())
                if r.get("thread_id") == thread_id and isinstance(r.get("seq"), int)]
    except (OSError, ValueError):
        return None
    return rows[-1]["seq"] if rows else None


FORMS = (
    ("1", "single_tool", form1_messages, judge_single),
    ("2", "parallel_two", form2_messages, judge_parallel),
    ("3", "long_observation_recovery", form3_messages, judge_long_recovery),
)


def run_forms(gateway_factory, *, out_dir: Path, batch_id: str,
              thread_prefix: str) -> dict:
    """gateway_factory(capture) -> 挂好 capture transport 的 ModelGateway。

    每形态独立 capture、独立线程 id（p25t3-p2-postN），便于逐 POST 对账。
    """
    out_dir = Path(out_dir)
    verdict: dict = {"probe": "p2_native_tooluse", "batch_id": batch_id,
                     "captured_at_iso": _utcnow(), "stage": STAGE, "forms": {}}
    for idx, (post, name, build_msgs, judge) in enumerate(FORMS):
        thread_id = f"{thread_prefix}-post{post}"
        capture: dict = {}
        gateway = gateway_factory(capture)
        messages = build_msgs()
        req_record = {"post": int(post), "form": name,
                      "captured_at_iso": _utcnow(), "stage": STAGE,
                      "request_kwargs": {"messages": messages,
                                         "max_tokens": MAX_TOKENS,
                                         "temperature": TEMPERATURE,
                                         "timeout_s": TIMEOUT_S,
                                         "tools_injected": [
                                             t["function"]["name"]
                                             for t in TOOLS_SPEC]}}
        gateway_text: "str | None" = None
        gateway_usage = None
        gateway_exc: "str | None" = None
        try:
            with billing_context(task=TASK, batch_id=batch_id,
                                 thread_id=thread_id):
                gateway_text, gateway_usage = gateway.call(
                    STAGE, messages, max_tokens=MAX_TOKENS,
                    temperature=TEMPERATURE, timeout=TIMEOUT_S)
        except NonRetryableError as exc:
            gateway_exc = f"{type(exc).__name__}: {str(exc)[:500]}"
            outcome = "supplier_rejected"
        except Exception as exc:  # noqa: BLE001 —— 含 EmptyResponseError（tool-only 200）
            gateway_exc = f"{type(exc).__name__}: {str(exc)[:500]}"
            outcome = "gateway_exit" if capture.get("raw") else "error"
        else:
            outcome = "ok"

        raw = capture.get("raw") or {}
        # 判读一律以捕获的供应侧原始响应为准（ok 与 gateway_exit 同口径）；
        # 长观察恢复形态只认真文本（tool-only 200 自然缺标记判负）。
        judged = judge(raw) if raw else \
            {"pass": False, "reasons": ["无响应原文（供应侧未返回 200）"],
             "calls": []}
        seq = last_thread_seq(thread_id)
        resp_record = {"post": int(post), "form": name, "outcome": outcome,
                       "gateway_exception": gateway_exc,
                       "gateway_text": gateway_text,
                       "gateway_usage": ({"input_tokens": gateway_usage[0],
                                          "output_tokens": gateway_usage[1]}
                                         if gateway_usage else None),
                       "captured_req": capture.get("req_kwargs"),
                       "captured_raw_response": raw,
                       "judgment": {k: v for k, v in judged.items()
                                    if k != "feedback_messages"},
                       "ledger_seq": seq}
        req_record["captured_req_kwargs"] = capture.get("req_kwargs")
        write_evidence(out_dir / f"p2_post{post}_req.json", req_record)
        write_evidence(out_dir / f"p2_post{post}_resp.json", resp_record)
        passed = outcome in ("ok", "gateway_exit") and judged["pass"]
        verdict["forms"][post] = {"form": name, "outcome": outcome,
                                  "judgment_pass": passed, "seq": seq,
                                  "gateway_exception": gateway_exc}
        if not passed:
            verdict["p2_pass"] = False
            verdict["stop_reason"] = f"post{post}_failed"
            write_evidence(out_dir / "p2_verdict.json", verdict)
            # 提前失败即停，不补呼：供应商拒绝 rc=10，其余判负/错误分列。
            rc = EXIT_REJECTED if outcome == "supplier_rejected" else \
                EXIT_ERROR if outcome == "error" else EXIT_FAIL
            return {"rc": rc, "verdict": verdict}

    verdict["p2_pass"] = True
    verdict["stop_reason"] = "completed"
    write_evidence(out_dir / "p2_verdict.json", verdict)
    return {"rc": 0, "verdict": verdict}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_p2_native_tooluse.py",
        description="P2 Anthropic 原生 tool_use 探针（≤3 POST，显式付费守卫）")
    p.add_argument("--batch-id", required=True, help="配额票号（P25-B1）")
    p.add_argument("--thread-prefix", default="p25t3-p2")
    p.add_argument("--out-dir", default=None)
    return p


def main(argv: "list[str] | None" = None) -> int:
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("probe_p2: 拒绝运行——本探针至多 3 次真实付费 POST，必须显式设置 "
              "SIMPRAXIS_ALLOW_PAID=1（裸跑守卫，零触网 exit 2）", file=sys.stderr)
        return EXIT_GUARD
    args = build_parser().parse_args(argv)
    try:
        validate_batch_id(args.batch_id)
        validate_thread_id(args.thread_prefix)
    except ValueError as exc:
        print(f"probe_p2: 参数非法：{exc}", file=sys.stderr)
        return EXIT_GUARD
    out_dir = Path(args.out_dir) if args.out_dir else \
        phase25_root() / "probes" / "stage0"

    def _factory(capture: dict) -> ModelGateway:
        return ModelGateway(ledger=QuotaLedger.phase25(),
                            max_attempts=MAX_ATTEMPTS,
                            transport=make_tools_transport(capture))

    result = run_forms(_factory, out_dir=out_dir, batch_id=args.batch_id,
                       thread_prefix=args.thread_prefix)
    rc = result["rc"]
    print(f"probe_p2: rc={rc} forms={result['verdict']['forms']} 证据→{out_dir}",
          file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main())
