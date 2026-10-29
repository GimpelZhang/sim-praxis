#!/usr/bin/env python3
"""Phase 2.5 Task3 Stage0 — P5 GatewayModel 最小探针 + CodeAgent 产出率冒烟
（AC-T3-6，0 付费、0 新增 seq）。

**一次性探针，不是生产路径**（红线同 probe_p1：本文件唯一调用点，
永不 import orchestrator.graph/orchestrator.agentic；Task4 产品件
orchestrator/agentic/model.py 是独立交付）。两件事：

  1. _ProbeGatewayModel(smolagents.Model，由 build_probe_model_class() 惰性
     构造)：Task4 GatewayModel 的最小探针版，单方法缝
     generate(messages)→ChatMessage，内部仅
     ModelGateway.call("coding_debug", ...)——零 model_id/供应商 URL、
     CodeAgent 路径永不向网关传 tools（tools_to_call_from 非 None 即拒）、
     零 image block、response_format/stop_sequences 非 None 即拒（shim 缺口，
     见离线报告的 shim 依赖清单）。缝成立由 mock 网关 hermetic 单测证明。
  2. --offline-p1：复用 P1 两呼的 req/resp **原文证据件**做离线对照，零新增
     POST/seq：量化 CodeAgent 默认 <code></code> 标签产出率与 markdown
     代码围栏可抽取率（smolagents.utils.parse_code_blobs 同款解析器），
     并核 POST2 stop 哨兵遵从。

纪律：探针不直接发起付费（无网关构造、无网络 import 热路径）；按 AC-T3-8
统一守 SIMPRAXIS_ALLOW_PAID=1（裸跑 exit 2），离线模式同样要求——它是
探针统一显式运行开关，本脚本任何模式都不触网。

退出码：0=离线分析完成/缝冒烟通过；2=守卫/参数错；1=证据件缺失或分析失败。
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

from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.common.runtime_paths import phase25_root  # noqa: E402
from orchestrator.common.validators import validate_thread_id  # noqa: E402

STAGE = "coding_debug"           # 硬规则 1：唯一文本 stage
EXIT_GUARD = 2
EXIT_ERROR = 1
STOP_SENTINEL = "<<<DONE>>>"
CODE_TAGS = ("<code>", "</code>")
MARKDOWN_TAGS = ("```(?:python|py)", "\n```")


# ---------------------------------------------------------------------------
# _ProbeGatewayModel：Task4 GatewayModel 的最小探针版（单方法缝）
# ---------------------------------------------------------------------------

def build_probe_model_class():
    """惰性构造并返回 _ProbeGatewayModel 类（直接子类化安装期 smolagents.Model）。

    惰性 import：无 smolagents 环境仅 import 本模块不应失败；测试直接调本工厂。
    """
    from smolagents.models import ChatMessage, MessageRole, Model, TokenUsage  # noqa: PLC0415

    class _ProbeGatewayModel(Model):
        """smolagents Model → 方舟 coding_debug 文本 stage 的单方法缝。

        与 Task4 产品件同契约（AC-T4-1 预演）：generate 内只允许
        gateway.call("coding_debug", ...)；不传 tools、不构造 image block、
        零供应商信息。model_id=None → supports_stop_parameter=False，
        smolagents 不会下传 stop；若调用方强传 stop_sequences/response_format
        或 tools_to_call_from，说明走到 shim 不支持的路径，立即 fail-fast。
        """

        def __init__(self, gateway, *, stage: str = STAGE,
                     max_tokens: int = 4096, temperature: float = 0):
            super().__init__(flatten_messages_as_text=True)
            self.gateway = gateway
            self.stage = stage
            self.max_tokens = max_tokens
            self.temperature = temperature
            self.calls: list[dict] = []  # 测试观察点：每次 generate 的透传形状

        @staticmethod
        def _flatten(messages) -> list[dict]:
            out: list[dict] = []
            for m in messages:
                role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
                content = getattr(m, "content", None)
                if content is None and isinstance(m, dict):
                    content = m.get("content")
                role_value = role.value if hasattr(role, "value") else role
                if role_value in ("tool-call", "tool-response"):
                    raise ValueError(
                        f"CodeAgent 文本路径不应出现 {role_value} 角色"
                        "（原生 tool 往返是 ToolCallingAgent 形态，非本缝）")
                if isinstance(content, list):
                    parts: list[str] = []
                    for b in content:
                        if not isinstance(b, dict):
                            parts.append(str(b))
                            continue
                        btype = b.get("type")
                        if btype == "image":
                            raise ValueError("硬规则 1：coding 阶段禁止 image block")
                        if btype in ("text", None):
                            parts.append(str(b.get("text", "")))
                        else:
                            raise ValueError(
                                f"不支持的 content block 类型 {btype}（仅 text）")
                    text = "\n".join(p for p in parts if p)
                elif content is None:
                    tool_calls = getattr(m, "tool_calls", None)
                    if tool_calls:
                        raise ValueError("CodeAgent 文本路径不应携带 tool_calls")
                    text = ""
                else:
                    text = str(content)
                out.append({"role": role_value,
                            "content": [{"type": "text", "text": text}]})
            return out

        def generate(self, messages, stop_sequences=None,
                     response_format=None, tools_to_call_from=None, **kwargs):
            if tools_to_call_from is not None:
                raise ValueError(
                    "CodeAgent 路径永不向网关传 tools（AC-T4-1）；"
                    "tools_to_call_from 非 None 是 ToolCallingAgent 形态")
            if stop_sequences is not None:
                raise ValueError(
                    "shim 缺口：ModelGateway.call 不支持 stop_sequences"
                    "（P5 报告 shim 依赖清单项；CodeAgent 靠哨兵解析）")
            if response_format is not None:
                raise ValueError(
                    "shim 缺口：ModelGateway.call 不支持 response_format")
            if kwargs.get("tools") is not None:
                raise ValueError("CodeAgent 路径不得透传 tools kwarg")
            payload = self._flatten(messages)
            self.calls.append({"stage": self.stage, "messages": payload})
            text, usage = self.gateway.call(
                self.stage, payload, max_tokens=self.max_tokens,
                temperature=self.temperature)
            tin, tout = usage
            token_usage = None
            if isinstance(tin, int) and isinstance(tout, int):
                token_usage = TokenUsage(input_tokens=tin, output_tokens=tout)
            return ChatMessage(role=MessageRole.ASSISTANT, content=text,
                               raw={"stage": self.stage,
                                    "evidence_ref": "probes/stage0/"
                                                   "p5_offline_analysis.json"},
                               token_usage=token_usage)

    return _ProbeGatewayModel


# ---------------------------------------------------------------------------
# 离线对照：P1 原文 → stop 遵从 + <code> 可抽取率（零新增 seq）
# ---------------------------------------------------------------------------

def code_extract_metrics(text: str) -> dict:
    """对一段响应原文测两种解析器的可抽取性（不调模型）。"""
    from smolagents.utils import parse_code_blobs  # noqa: PLC0415

    def _try(tags):
        try:
            blob = parse_code_blobs(text or "", tags)
            return {"extractable": True, "chars": len(blob)}
        except ValueError:
            return {"extractable": False, "chars": 0}

    literal = re.findall(r"<code>(.*?)</code>", text or "", flags=re.DOTALL)
    return {
        "literal_code_tags": len(literal),
        "code_tag_parser": _try(CODE_TAGS),
        "markdown_fallback_parser": _try(MARKDOWN_TAGS),
    }


def analyze_p1_artifacts(p1_dir: Path) -> dict:
    p1_dir = Path(p1_dir)
    posts = {}
    texts = {}
    for post in (1, 2):
        fp = p1_dir / f"p1_post{post}_resp.json"
        if not fp.is_file():
            raise FileNotFoundError(f"P1 证据件缺失：{fp}")
        doc = json.loads(fp.read_text(encoding="utf-8"))
        text = doc.get("response_text") or ""
        texts[post] = text
        entry = {"outcome": doc.get("outcome"),
                 "ledger_seq": doc.get("ledger_seq"),
                 "response_chars": len(text),
                 **code_extract_metrics(text)}
        if post == 2:
            tail = text.rstrip()
            entry["stop_sentinel_present"] = STOP_SENTINEL in text
            entry["stop_sentinel_at_tail"] = tail.endswith(STOP_SENTINEL)
            entry["judgment"] = doc.get("judgment")
        posts[str(post)] = entry

    n = len(texts)
    code_rate = sum(1 for k in texts
                    if posts[str(k)]["code_tag_parser"]["extractable"]) / n
    md_rate = sum(1 for k in texts
                  if posts[str(k)]["markdown_fallback_parser"]["extractable"]) / n
    post2 = posts["2"]
    return {
        "probe": "p5_gatewaymodel_smoke",
        "mode": "offline_p1",
        "captured_at_iso": _utcnow(),
        "new_paid_posts": 0,
        "sample_size": n,
        "code_tag_extractable_rate": code_rate,
        "markdown_code_extractable_rate": md_rate,
        "post2_stop_compliance": bool(post2.get("stop_sentinel_at_tail")),
        "posts": posts,
        # shim 依赖清单（P1 挂 P2 过时 Task4 切 ToolCallingAgent 的前置事实）：
        "shim_dependencies": [
            "ModelGateway.call 无 tools/tool_choice 透传（P2 探针经 transport="
            "注入点验证；产品件需另行裁决，本探针不改产品代码）",
            "ModelGateway.call 无 stop_sequences 参数（CodeAgent 靠哨兵解析）",
            "ModelGateway.call 无 response_format/JSON mode",
            "tool-only 200 触发 G-12 空回答退避/记账口径（max_attempts=1 下 "
            "ok=false），ToolCallingAgent 产品件须按 tool_calls 判成功",
        ],
    }


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_evidence(path: Path, payload: dict) -> None:
    ensure_private_dir(path.parent)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", mode=0o600)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_p5_gatewaymodel_smoke.py",
        description="P5 GatewayModel 缝冒烟 + 复用 P1 原文离线对照（0 付费）")
    p.add_argument("--offline-p1", metavar="DIR", required=True,
                   help="P1 证据目录（含 p1_post1_resp.json/p1_post2_resp.json）")
    p.add_argument("--thread", default="p25t3-p5")
    p.add_argument("--out", default=None,
                   help="输出 JSON（默认证据目录/p5_offline_analysis.json）")
    return p


def main(argv: "list[str] | None" = None) -> int:
    # AC-T3-8 统一双守卫：本脚本不触网，显式开关仍为探针运行的统一门禁。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("probe_p5: 拒绝运行——探针统一显式运行开关 SIMPRAXIS_ALLOW_PAID=1"
              "（本脚本 0 付费；裸跑 exit 2）", file=sys.stderr)
        return EXIT_GUARD
    args = build_parser().parse_args(argv)
    try:
        validate_thread_id(args.thread)
    except ValueError as exc:
        print(f"probe_p5: 参数非法：{exc}", file=sys.stderr)
        return EXIT_GUARD
    out = Path(args.out) if args.out else \
        phase25_root() / "probes" / "stage0" / "p5_offline_analysis.json"
    try:
        report = analyze_p1_artifacts(Path(args.offline_p1))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"probe_p5: 离线分析失败：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_ERROR
    report["thread_id"] = args.thread
    write_evidence(out, report)
    print(f"probe_p5: 离线对照完成 code_tag_rate={report['code_tag_extractable_rate']} "
          f"md_rate={report['markdown_code_extractable_rate']} "
          f"stop={report['post2_stop_compliance']} 证据→{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
