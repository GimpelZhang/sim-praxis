#!/usr/bin/env python3
"""T6：12 请求协议校准（先预登记、后固定顺序执行；失败即停不补考）。

两段式，都能离线复算：

* ``--pre-register``：把 6 槽 / 12 个**完整发网件**（含 nonce 派生标记、合成公开
  记录、工具 schema、max_output）与逐件计数写成 ``analysis/t6_protocol/protocol_prereg.json``。
  不触网、不付费。nonce 由 campaign id + slot 确定性派生，故预登记件可被独立重算。
* ``--run``：只在 rev 3 LOCK 生效后运行。每 POST 前 PaidTransport 重算 LOCK 事实，
  ledger preflight 过了才发；长链前先由前 6 个成功 POST 的 usage 建立供应商开销带
  （区间证明不了就 **零长链 POST** 记 BLOCKED_CONTEXT_MEASUREMENT）。

安全：凭据只从 env 按名读取，绝不回显/入件/入日志；每次 POST 的请求体 hash、
HTTP 状态、provider request_id、stop_reason、usage、时延与响应哈希落 capture 件。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common import runtime_paths as RP
from orchestrator.gateway.quota_ledger import QuotaLedger
from orchestrator.rsi_r01 import attempts as AT
from orchestrator.rsi_r01 import campaign as C
from orchestrator.rsi_r01 import lock as L
from orchestrator.rsi_r01 import paid as P
from orchestrator.rsi_r01 import protocol as PR

STAGE = "coding_debug"                       # 工作负载模型所在 stage（模型名仍从 yaml 读）
TASK = 0                                     # RSI_R01_TASK_CAPS[0] = 12 = 协议帽
BATCH_ID = "R01-T6-protocol-1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ROOT = RP.rsi_r01_root()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """任何 3xx 一律当失败：绝不把 POST 悄悄改写成别的请求重发。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code,
                                     f"redirect refused ({newurl})", headers, fp)


_OPENER = urllib.request.build_opener(NoRedirect)


def _load_ctx(args: argparse.Namespace) -> C.CampaignContext:
    if args.campaign_root:
        return C.load_campaign(campaign_id=Path(args.campaign_root).name,
                               root=Path(args.campaign_root).parent)
    if not args.campaign_id:
        raise SystemExit("需要 --campaign-id 或 --campaign-root")
    return C.load_campaign(campaign_id=args.campaign_id)


def _model() -> str:
    info = L.stage_models(_REPO_ROOT / "orchestrator/gateway/stage_models.yaml")
    model = (info.get("models") or {}).get(STAGE)
    if not model:
        raise SystemExit(f"stage_models.yaml 缺 {STAGE} 的 model_id（硬规则 1）")
    return str(model)


def _tokenizer(args: argparse.Namespace) -> PR.Tokenizer:
    if not args.tokenizer or not args.tokenizer_sha256:
        raise SystemExit("--run/--pre-register 需要 --tokenizer 与 --tokenizer-sha256")
    return PR.Tokenizer.load(args.tokenizer, args.tokenizer_sha256)


def _nonces(ctx: C.CampaignContext) -> "dict[int, str]":
    """确定性 nonce：campaign id + slot ⇒ 预登记件可被第三方逐字重算。"""
    import hashlib
    return {slot: hashlib.sha256(
        f"{ctx.campaign_id}:protocol:{slot}".encode("utf-8")).hexdigest()[:16]
        for slot, _, _, _ in PR.CHAIN}


def _ensure_batch_approval(ctx: C.CampaignContext) -> str:
    """本批次的 ledger 书面凭据（Agent 自批；硬规则 2 授权，逐次记账）。"""
    rel = f"quota/approvals/{BATCH_ID}.json"
    path = ctx.root / rel
    if path.is_file():
        return rel
    C.write_json(ctx, rel, {
        "schema": "simpraxis.rsi_r01.batch_approval.v1",
        "batch_id": BATCH_ID,
        "task": TASK,
        "stage": STAGE,
        "approved": PR.PROTOCOL_POSTS,
        "beyond_normal": False,
        "approved_by": "agent_self_approval",
        "basis": ("硬规则 2 + 用户 2026-11-09 执行授权：协议批次 12 POST；"
                  "Agent 自批不冒充用户逐笔批准，逐次记账"),
        "campaign_id": ctx.campaign_id,
        "created_utc": C._utc_now(),
    })
    return rel


def _creds() -> "tuple[str, str]":
    token = os.environ.get("SIMPRAXIS_LLM_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        raise SystemExit("缺少凭据：先 source scripts/00_env.sh（值不回显）")
    base = (os.environ.get("SIMPRAXIS_LLM_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL"))
    if not base:
        raise SystemExit("缺少 base_url：先 source scripts/00_env.sh")
    return token, base


def _http_sender(*, token: str, base_url: str, timeout_s: int,
                 ctx: C.CampaignContext, ledger: QuotaLedger, model: str,
                 capture_rel: str):
    """一逻辑请求一 POST；无重试、无重定向；每 POST 前后各记一次账。"""
    import hashlib

    url = base_url.rstrip("/") + "/v1/messages"
    captures: "list[dict[str, Any]]" = []

    def flush() -> None:
        C.write_json(ctx, capture_rel, {
            "schema": "simpraxis.rsi_r01.protocol_capture.v1",
            "checked_at_utc": C._utc_now(),
            "endpoint_host": urllib.parse.urlsplit(url).netloc,
            "note": "不记录任何请求头与凭据；只记状态/ID/usage/哈希/时延/安全摘要",
            "posts": captures,
        })

    def sender(plan):  # noqa: ANN001 —— T.Sender 协议
        ledger.preflight(STAGE, task=TASK, batch_id=BATCH_ID)
        payload = dict(plan.request)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            # 认证头只在这里出现一次，不进任何落盘件
            "Authorization": f"Bearer {token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        })
        started = time.time()
        try:
            with _OPENER.open(req, timeout=timeout_s) as resp:
                status = int(getattr(resp, "status", 200))
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read() or b""
        except Exception as exc:  # noqa: BLE001 —— 发送状态不确定：记账后原样抛
            ledger.record(STAGE, task=TASK, batch_id=BATCH_ID, ok=False,
                          model=model,
                          err=f"UNKNOWN_SENT: {type(exc).__name__}: {exc}")
            captures.append({
                "request_sha256": plan.sha256, "request_bytes": len(data),
                "http_status": None, "provider_request_id": None,
                "stop_reason": None, "usage": None,
                "latency_s": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "unknown_sent": True,
            })
            flush()
            raise
        latency = round(time.time() - started, 3)
        body: "dict[str, Any]" = {}
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
            if isinstance(parsed, Mapping):
                body = dict(parsed)
        except Exception:  # noqa: BLE001 —— 非 JSON 回包照实记
            body = {}
        blocks = body.get("content") or []
        text = "".join(str(b.get("text", "")) for b in blocks
                       if isinstance(b, Mapping) and b.get("type") == "text")
        usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else None
        ok = 200 <= status < 300
        err = None
        if not ok:
            snippet = (body.get("error") or {}).get("message") if isinstance(
                body.get("error"), Mapping) else None
            err = f"HTTP {status}: {str(snippet or raw[:200].decode('utf-8', 'replace'))[:300]}"
        ledger.record(STAGE, task=TASK, batch_id=BATCH_ID, ok=ok, model=model,
                      tokens_in=(usage or {}).get("input_tokens"),
                      tokens_out=(usage or {}).get("output_tokens"),
                      err=err,
                      evidence=f"analysis/t6_protocol/{plan.sha256[:16]}")
        captures.append({
            "request_sha256": plan.sha256, "request_bytes": len(data),
            "http_status": status,
            "provider_request_id": body.get("id") if ok else None,
            "stop_reason": body.get("stop_reason"),
            "usage": dict(usage) if usage else None,
            "latency_s": latency,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "response_bytes": len(raw),
            "block_types": [b.get("type") for b in blocks if isinstance(b, Mapping)],
            "text_len": len(text),
            "text_head": text[:2000],
            "error": err,
            "unknown_sent": False,
        })
        flush()
        meta = {"content_blocks": blocks, "http_status": status,
                "request_id": body.get("id"), "stop_reason": body.get("stop_reason"),
                "usage": dict(usage) if usage else None,
                "latency_s": latency}
        return meta, text
    return sender


def main(argv: "Sequence[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 T6：12 请求协议校准")
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--campaign-root", default=None)
    ap.add_argument("--pre-register", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--tokenizer", default=None, help="本机钉死分词器 tokenizer.json")
    ap.add_argument("--tokenizer-sha256", default=None)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args(argv)
    if not args.pre_register and not args.run:
        ap.error("需要 --pre-register 或 --run")
    ctx = _load_ctx(args)
    os.environ["SIMPRAXIS_RSI_R01_CAMPAIGN"] = ctx.campaign_id  # 账/槽与本 campaign 同步
    model = _model()
    if args.pre_register:
        tok = _tokenizer(args)
        runner = PR.ProtocolRunner(ctx=ctx, model=model, tokenizer=tok,
                                   transport=P.PaidTransport(ctx=ctx), budget=None)
        doc = runner.write_prereg(nonces=_nonces(ctx))
        summary = {
            "prereg": runner.prereg_relpath(),
            "model": model, "post_count": doc["post_count"],
            "tokenizer": doc.get("tokenizer"),
            "posts": [{"slot": p["slot"], "chain": p["chain"],
                       "index": p["index"], "interval": p["interval"],
                       "bytes": p["bytes"],
                       "tokenizer_tokens": p["tokenizer_tokens"],
                       "request_sha256": p["request_sha256"]}
                      for p in doc["posts"]],
        }
        C.write_json(ctx, "analysis/t6_protocol/prereg_summary.json", summary)
        print(json.dumps({"ok": True, "prereg": runner.prereg_relpath(),
                          "posts": doc["post_count"], "model": model},
                         ensure_ascii=False))
        return 0
    # --run ------------------------------------------------------------
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        raise SystemExit("付费运行需 SIMPRAXIS_ALLOW_PAID=1（防裸跑触网）")
    tok = _tokenizer(args)
    approval_rel = _ensure_batch_approval(ctx)
    token, base_url = _creds()
    ledger = QuotaLedger.rsi_r01()
    budget = AT.SlotBudget(AT.quota_paths(ctx.root)["quota"] / "slots_protocol.json")
    capture_rel = "analysis/t6_protocol/capture.json"
    runner = PR.ProtocolRunner(ctx=ctx, model=model, tokenizer=tok,
                               transport=P.PaidTransport(ctx=ctx), budget=budget)
    report = runner.run(
        sender=_http_sender(token=token, base_url=base_url, timeout_s=args.timeout,
                            ctx=ctx, ledger=ledger, model=model,
                            capture_rel=capture_rel),
        tool=PR.ToolExec(root=ctx.subdir("analysis") / "t6_protocol" / "tools"))
    print(json.dumps({"verdict": report["verdict"],
                      "blocked_code": report["blocked_code"],
                      "posts_sent": report["posts_sent"],
                      "overhead_band": report["overhead_band"],
                      "report": runner.report_relpath(),
                      "approval": approval_rel,
                      "ledger": str(RP.rsi_r01_ledger_path())},
                     ensure_ascii=False, indent=1))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
