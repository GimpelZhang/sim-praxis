#!/usr/bin/env python3
"""Phase 2.5 Task3 Stage0 — P3 失败体恢复探针（AC-T3-4，≤1 POST）。

**一次性探针，不是生产路径**（红线同 probe_p1：本文件是唯一调用点，
永不 import orchestrator.graph/orchestrator.agentic）。回答一个事实：
失败体能否被分类为「供应商错（不该自修）」vs「模型可自修错（下一步循环
可恢复）」，且恢复不破坏下一步。

三类合成失败（坏 JSON / 超长截断 / 429）：
  - 坏 JSON、429 两类**只用 mock 网关** hermetic 单测（tests/milestone25/
    test_task3_probes.py），不触网、不占 seq；
  - 真实付费侧至多触发 **1 类 = 超长截断**（--trigger truncation，1 POST）：
    要模型在 max_tokens=512 内输出 200 元素 JSON 数组，供应侧大概率 200
    到顶（finish_reason=length、JSON 不闭合）——这是「模型可自修错」的
    真实样本：加大预算/续作即可恢复，与供应商错无关。**不发第 2 呼做
    恢复**（恢复循环本身由 mock 单测证明）；任何供应侧出口原样留存占 seq。

分类判据（预注册，classify_failure 纯函数，单测逐条锁）：
  - 429/5xx/401/403/404 或 litellm RateLimitError/NonRetryableError 类 →
    supplier（429=supplier_retryable 走退避预算，绝不靠改提示词）；
  - HTTP 200 + finish_reason=length + JSON 不完整 → self_repairable_truncation；
  - HTTP 200 + JSON 解析失败但长度未到顶 → self_repairable_bad_json
    （下一步给一次格式纠偏；不重试同一请求）。

纪律：SIMPRAXIS_ALLOW_PAID=1 首行守卫（裸跑 exit 2 零触网）；paid marker；
ModelGateway(ledger=QuotaLedger.phase25(), max_attempts=1) 单发（经
transport= 既有注入点捕获 finish_reason，同 probe_p2 锁内适配）；
billing_context(task=3, batch_id=P25-B1)；req/resp/异常原文 0600 落
$SIMPRAXIS_PHASE25_ROOT/probes/stage0/；零 image block、零 model_id 参数。

退出码：0=成功观测到预期失败类并完成分类；2=守卫/参数错；10=供应侧明确
拒绝（NonRetryableError，原样留存）；1=其他停止出口；3=观测到 200 但
截断判据未命中（事实与预期不符，原样留存，不调参翻案）。
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
TRIGGER_MAX_TOKENS = 512
TRIGGER_N_ITEMS = 200
TEMPERATURE = 0
TIMEOUT_S = 300

EXIT_GUARD = 2
EXIT_REJECTED = 10
EXIT_ERROR = 1
EXIT_FAIL = 3

SUPPLIER_RETRYABLE = "supplier_retryable"     # 429/5xx：退避预算，非提示词问题
SUPPLIER_FATAL = "supplier_fatal"             # 401/403/404/400：停手，规则 1 通道
SELF_TRUNCATION = "self_repairable_truncation"
SELF_BAD_JSON = "self_repairable_bad_json"
OK = "ok"


# ---------------------------------------------------------------------------
# 纯分类逻辑（hermetic 单测全覆盖）
# ---------------------------------------------------------------------------

def json_array_completeness(text: str) -> str:
    """返回 complete / incomplete / absent：模型 JSON 数组形态机判。"""
    s = (text or "").strip()
    if not s:
        return "absent"
    try:
        v = json.loads(s)
        return "complete" if isinstance(v, list) else "absent"
    except json.JSONDecodeError:
        pass
    # 去围栏再试一次（模型合法包了 ```json 但被截断时围栏也不闭）
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
    i, j = s.find("["), s.rfind("]")
    if i < 0:
        return "absent"
    if j > i:
        try:
            v = json.loads(s[i:j + 1])
            if isinstance(v, list):
                return "complete"
        except json.JSONDecodeError:
            pass
    return "incomplete"


def classify_failure(*, http_status: "int | None" = None,
                     finish_reason: "str | None" = None,
                     text: "str | None" = None,
                     exc_name: "str | None" = None) -> dict:
    """失败体分类（判据预注册，纯函数）。

    供应侧异常优先（exc_name/http 4xx5xx）；200 才看模型体。返回
    {cls, retryable_in_next_step, reason}。
    """
    exc = exc_name or ""
    if exc == "RateLimitError" or http_status == 429:
        return {"cls": SUPPLIER_RETRYABLE, "self_repairable": False,
                "reason": "429 限流：走退避/长熔断，不改提示词、不重试到成功"}
    if exc == "NonRetryableError" or (http_status is not None
                                      and http_status in (400, 401, 403, 404)):
        return {"cls": SUPPLIER_FATAL, "self_repairable": False,
                "reason": f"{http_status or exc}：供应侧明确拒绝，停手走规则 1 通道"}
    if http_status is not None and 500 <= http_status < 600:
        return {"cls": SUPPLIER_RETRYABLE, "self_repairable": False,
                "reason": f"{http_status}：供应侧 5xx，退避预算内重试"}
    if http_status == 200 or (http_status is None and exc == ""):
        completeness = json_array_completeness(text or "")
        if finish_reason == "length" and completeness == "incomplete":
            return {"cls": SELF_TRUNCATION, "self_repairable": True,
                    "reason": "200 到顶且 JSON 不闭合：加大 max_tokens/续作可恢复"}
        if completeness == "incomplete" or (
                completeness == "absent" and (text or "").strip()):
            return {"cls": SELF_BAD_JSON, "self_repairable": True,
                    "reason": "200 体非合法 JSON：下一步一次格式纠偏可恢复"}
        if completeness == "complete":
            return {"cls": OK, "self_repairable": False,
                    "reason": "200 体为完整 JSON 数组：无失败"}
    return {"cls": "unknown", "self_repairable": False,
            "reason": f"未归类出口 exc={exc} status={http_status}：按供应商错保守处理"}


def repair_request(messages: "list[dict]", prior_text: str) -> "list[dict]":
    """可自修错的下一步：附助手残体 + 一条纠偏 user 消息（不复发同一请求）。"""
    return [*messages,
            {"role": "assistant", "content": [
                {"type": "text", "text": prior_text}]},
            {"role": "user", "content": [{"type": "text",
                "text": "上一响应不是闭合的 JSON 数组（被截断或格式错误）。"
                        "不要解释，重新输出完整的 JSON 数组。"}]}]


# ---------------------------------------------------------------------------
# 传输缝（同 probe_p2 锁内适配：transport= 注入点，捕获 finish_reason）
# ---------------------------------------------------------------------------

def _call_completion(**kwargs: Any) -> Any:
    import litellm  # noqa: PLC0415
    return litellm.completion(**kwargs)


def _snapshot(resp: Any) -> dict:
    if hasattr(resp, "model_dump"):
        try:
            return json.loads(json.dumps(resp.model_dump(), default=str))
        except Exception:  # noqa: BLE001
            pass
    ch = getattr(resp, "choices", [None])[0]
    return {"choices": [{"finish_reason": getattr(ch, "finish_reason", None),
                         "message": {"content": getattr(getattr(ch, "message", None),
                                                         "content", None)}}]}


def make_capture_transport(capture: dict):
    def _transport(**kwargs: Any) -> Any:
        kwargs = dict(kwargs)
        resp = _call_completion(**kwargs)
        capture["req_kwargs"] = {k: v for k, v in kwargs.items()
                                 if k != "api_key"}
        capture["raw"] = _snapshot(resp)
        return resp
    return _transport


def _finish_reason(raw: dict) -> "str | None":
    try:
        return raw["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


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


def build_trigger_messages() -> list[dict]:
    user = ("传输失败体测试，不写产品代码。只输出一个 JSON 数组，包含 "
            f"{TRIGGER_N_ITEMS} 个对象，每个形如 "
            '{"i": 1, "v": "abcdefghijklmnopqrstuvwxyz0123456789"}，"i" '
            f"从 1 递增到 {TRIGGER_N_ITEMS}。不要 markdown、不要解释、"
            "不要省略任何元素。")
    return [{"role": "user", "content": [{"type": "text", "text": user}]}]


def run_truncation(gateway, capture: dict, *, out_dir: Path, batch_id: str,
                   thread_id: str) -> dict:
    """1 POST 真实触发超长截断类（不恢复、不补呼）。

    capture 必须与挂到 ModelGateway(transport=) 的包装器是同一个 dict。
    """
    out_dir = Path(out_dir)
    messages = build_trigger_messages()
    record: dict = {"probe": "p3_failures", "trigger": "truncation",
                    "batch_id": batch_id, "thread_id": thread_id,
                    "captured_at_iso": _utcnow(), "stage": STAGE,
                    "request": {"messages": messages,
                                "max_tokens": TRIGGER_MAX_TOKENS,
                                "temperature": TEMPERATURE,
                                "timeout_s": TIMEOUT_S}}
    gateway_text: "str | None" = None
    gateway_usage = None
    try:
        with billing_context(task=TASK, batch_id=batch_id, thread_id=thread_id):
            gateway_text, gateway_usage = gateway.call(
                STAGE, messages, max_tokens=TRIGGER_MAX_TOKENS,
                temperature=TEMPERATURE, timeout=TIMEOUT_S)
    except NonRetryableError as exc:
        record.update(outcome="supplier_rejected",
                      error=f"{type(exc).__name__}: {str(exc)[:500]}",
                      ledger_seq=last_thread_seq(thread_id))
        write_evidence(out_dir / "p3_truncation.json", record)
        return {"rc": EXIT_REJECTED, "cls": SUPPLIER_FATAL}
    except Exception as exc:  # noqa: BLE001 —— 越过 preflight 的任何出口占 seq
        record.update(outcome="error",
                      error=f"{type(exc).__name__}: {str(exc)[:500]}",
                      captured_raw=capture.get("raw"),
                      ledger_seq=last_thread_seq(thread_id))
        write_evidence(out_dir / "p3_truncation.json", record)
        return {"rc": EXIT_ERROR, "cls": None}

    raw = capture.get("raw") or {}
    fr = _finish_reason(raw)
    text = gateway_text if gateway_text is not None else ""
    verdict = classify_failure(http_status=200, finish_reason=fr, text=text)
    record.update(outcome="ok", response_text=text, response_chars=len(text),
                  usage=({"input_tokens": gateway_usage[0],
                          "output_tokens": gateway_usage[1]}
                         if gateway_usage else None),
                  finish_reason=fr, captured_raw=raw,
                  classification=verdict, ledger_seq=last_thread_seq(thread_id))
    write_evidence(out_dir / "p3_truncation.json", record)
    # 预注册预期：self_repairable_truncation；命中即过，不命中照实判负不调参。
    if verdict["cls"] == SELF_TRUNCATION:
        return {"rc": 0, "cls": verdict["cls"]}
    if verdict["cls"] == OK:
        # 模型在 512 内输出了完整 200 元素数组（未触发截断）：事实与预期不符，
        # 占 1 seq，判 blocked/负——禁止调大 N 或调小预算再烧一呼翻案。
        record["note"] = "未观测到截断：512 token 内完整返回 200 元素数组"
        write_evidence(out_dir / "p3_truncation.json", record)
        return {"rc": EXIT_FAIL, "cls": OK}
    record["note"] = f"200 但分类={verdict['cls']}，非预注册截断类"
    write_evidence(out_dir / "p3_truncation.json", record)
    return {"rc": EXIT_FAIL, "cls": verdict["cls"]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_p3_failures.py",
        description="P3 失败体恢复探针（真实侧 ≤1 POST 触发截断类，显式付费守卫）")
    p.add_argument("--batch-id", required=True)
    p.add_argument("--thread", default="p25t3-p3")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--trigger", choices=["truncation"], default="truncation",
                   help="真实触发类（仅 truncation；坏 JSON/429 由 mock 单测覆盖）")
    return p


def main(argv: "list[str] | None" = None) -> int:
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("probe_p3: 拒绝运行——本探针发起至多 1 次真实付费 POST，必须显式"
              "设置 SIMPRAXIS_ALLOW_PAID=1（裸跑守卫，零触网 exit 2）",
              file=sys.stderr)
        return EXIT_GUARD
    args = build_parser().parse_args(argv)
    try:
        validate_batch_id(args.batch_id)
        validate_thread_id(args.thread)
    except ValueError as exc:
        print(f"probe_p3: 参数非法：{exc}", file=sys.stderr)
        return EXIT_GUARD
    out_dir = Path(args.out_dir) if args.out_dir else \
        phase25_root() / "probes" / "stage0"

    capture: dict = {}
    gateway = ModelGateway(ledger=QuotaLedger.phase25(),
                           max_attempts=MAX_ATTEMPTS,
                           transport=make_capture_transport(capture))
    result = run_truncation(gateway, capture, out_dir=out_dir,
                            batch_id=args.batch_id, thread_id=args.thread)
    print(f"probe_p3: rc={result['rc']} class={result['cls']} 证据→{out_dir}",
          file=sys.stderr if result["rc"] else sys.stdout)
    return result["rc"]


if __name__ == "__main__":
    sys.exit(main())
