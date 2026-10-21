#!/usr/bin/env python3
"""Phase 2 Task6：response_format 强制合法 JSON 一次性诊断探针（图外独立工具）。

**一次性探针，不是生产路径**：LangGraph 图/runner 永不导入或调用本脚本
（调用点由 tests/milestone2/test_diag_json_mode.py 源码红线扫描锁定，
仅本文件 1 处；本脚本也永不 import orchestrator.graph）。背景：拆块出生
（runner.py chunked_birth，信封 ``{"chunk":{"path","index","total","code"}}``）
run1–run3 中 22% 的 coding_debug 回包是非法 JSON（未转义分隔符、裸控制
字符）。用户 2026-10-21 裁决：先小额付费探针验方舟 Anthropic-Messages
shim（经 litellm 1.99.4 ``anthropic/`` provider，模型 deepseek-v4.1-flash，
``litellm.drop_params=True`` 全局开）是否支持 response_format 强制合法
JSON；不支持则引擎改 sentinel 围栏纯文本定界。

探针分两段：

  **A. 静态 transform 取证（零成本、零触网，纯函数）**：直接调 litellm
  客户端转换层 ``AnthropicConfig.map_openai_params(..., drop_params=True)``，
  对两种 response_format 形态分别取证 wire 参数：
    - ``{"type":"json_object"}``（无 schema）：LEAD 离线实测参数被客户端
      静默全丢弃（optional params 仅 max_tokens/temperature，无
      tools/tool_choice/output_format，json_mode 不置），wire 请求与普通
      调用等价——**该形态不需要付费即可判"不支持"**；
    - ``{"type":"json_schema","json_schema":{...}}``：litellm 把它翻译成
      强制 tool call（tools + tool_choice=json_tool_call + json_mode=True），
      方舟 shim 对 deepseek 模型是否接受且真的强制合法 JSON，需 B 段实证。
  litellm 升级后本形状若变化，单测红线立即报警。

  **B. 恰好 1 个付费单发**：经注入 transport 闭包在 litellm.completion
  kwargs 单点加 json_schema 形态（ledger/preflight/记账/暂停门全部照常），
  prompt 要求模型只输出生产拆块信封（path=genesis_humanoid_dr.py,
  index=3, total=4），code 为 4000–8000 字符、含 Windows 路径/正则反斜杠/
  三引号 docstring/单双引号/中文注释的合法 Python——复现 run1–run3 的
  转义压力。回包做四层严格核验：json.loads(strict 默认，裸控制字符即失败)、
  信封字段类型与取值、code 可 ast.parse、五个压力 marker 齐全且长度达标。

  **交付通道取证（HTTPHandler.post 层拦截）**：内容合规不足以证明交付
  走了 tool 通道——litellm 1.99.4 非流式 json_mode 把 tool_use 重序列化
  成普通 message.content 后，"真 tool 交付"与"模型无视 tool_choice 自觉
  吐 JSON"在 litellm 出口字面相同。曾尝试读
  ``model_response._hidden_params["original_response"]`` 取证，但该通道
  在 1.99.4 非流式路径是**死赋值**：transformation.py L2417 新建空 dict、
  L2492 把 content blocks 写进旧 dict、L2510 整体替换为空 dict（仅含
  additional_headers/provider_specific_fields）。此事实经 fresh reviewer
  真实管线 harness + LEAD 离线复现（三态 httpx.Response）双重确认，
  canary 测试钉死，litellm 修复后应回退到更省的 hidden_params 取证。
  故闭包在单次 completion() 期间临时 monkey-patch
  ``litellm.llms.custom_httpx.http_handler.HTTPHandler.post`` 类属性
  （同步 anthropic 非流式路径唯一 HTTP 出口，handler.py L584；不碰 async
  路径），从方舟**原始 httpx 响应**只取最小元数据（块类型/tool 名/状态/
  请求侧 tools 四布尔，绝不复制 input/text/code 原文），finally 必复原。
  本探针结论是 **n=1 的直接单发证据，支持接缝决策，不是强制力的数学
  证明**；历史 22% 非法 JSON 基率下，一次工具通道交付且压力内容合规
  即足以判定接缝可用，持续稳定性仍靠生产首验。

安全纪律（与既有 Task6 付费诊断探针同口径）：
  - 显式付费守卫：必须 ``SIMPRAXIS_ALLOW_PAID=1``，裸跑零触网 exit 2；
  - 硬规则 1：只允许 coding_debug 一个**文本** stage，绝不携带 image
    block；模型 ID 只读 stage_models.yaml（经 ModelGateway 正常读取），
    本脚本不接受任何模型覆盖参数；脚本内出现的 deepseek 模型名仅是 A 段
    转换探针的固定入参（_TRANSFORM_PROBE_MODEL），绝不传给网关；
  - **单发**：ModelGateway(max_attempts=1)，一次运行至多一次上游 HTTP
    （路由器未分类异常兜底分支极端情况至多补发一次，仍只记 1 seq）；
  - 运行前**必须先落本探针自己的书面票**（preflight 拦截，无票不触网）。
    **本票必须带 --beyond-normal**：开票时全局已用 ≥ NORMAL_BUDGET(60)，
    缺旗的票在 preflight（quota_ledger.py L427-432）会触发
    _write_budget_pause("batch_budget_reached")——持久化 stage="*" 全停
    熔断件 + seq=null blocked 后抛 GatewayPausedError，且同名票禁止覆盖，
    票号 T6-B8-probejson 一旦误用即废。beyond_normal 出票前置 used≥60
    且 used+approved≤HARD_CAP(104)，由 write_approval 强制。早先 32k
    上限探针出票时全局 <60 故不带旗，不可照抄：
    python -m orchestrator.gateway.quota_ledger approve \\
        --batch-id T6-B8-probejson --task 6 --stage coding_debug \\
        --beyond-normal --approved 1 \\
        --note "response_format json_schema 强制 JSON 单发探针"
  - 经 billing_context/QuotaLedger 正常记账（恰好一行整数 seq，失败也占
    seq；缺凭据/缺票/触顶 seq=null blocked 或 GatewayPausedError）；
  - 证据 JSON 0600 原子写落 phase2 runs/task6_ascent/：只存静态取证、
    usage、四层核验明细、raw 长度与脱敏 head/tail 300 字符、HTTPHandler
    拦截元数据（块类型/tool 名/请求侧四布尔/attempts，无任何 input/text/
    code 原文），不存认证头/完整原文；err 与 head/tail 经密钥正则 + URL
    剥离双重脱敏。--out 的父目录会被 ensure_private_dir 置 0700（已存在
    也 chmod，勿指 /tmp 等共享目录；生产用默认 phase2 runs 路径）。

退出码：0=HTTP 200、内容四层核验全过（enforced=True）**且** HTTPHandler
拦截证实方舟原始块走 tool_use 通道（块名恰为 EXPECTED_TOOL_NAME=
json_tool_call；名不符不新增 reason 字符串，落现有
content_failed_enforcement 分支），reason="tool_channel_delivered_n1"
——工具通道交付且压力内容合规的直接单发证据（n=1，支持接缝决策，
非数学证明强制力）；
11=HTTP 200 已接受但上述合取不成立，按 reason 区分：
text_compliant_voluntary（原始块全 text 而内容合规——恰是历史 7/9 自觉
基率情形）、content_failed_enforcement（内容层不过，含 tool_use 参数
非法 JSON 被 litellm 透传成串、或 tool_use 块名不是
EXPECTED_TOOL_NAME）、tool_input_null（tool_use 块 input
为 null；1.99.4 实测 litellm 产出字面串 "null"，内容层必不过）、
unattestable（200 但原始块无法解析/无块，通道不可证）；2=未设付费守卫
或参数错误；10=收到明确拒绝（NonRetryableError，附拦截到的 status 元
数据，证据已落盘）；1=其他停止出口——连接异常（无 200 截获，不落 HTTP
证据）、GatewayPausedError（缺票/任务或全局硬顶/长熔断）、
StageUnresolvedError、配置/凭据错误；原因看 stderr 与台账该行 err。
  200 后异常（如 thinking-only 空 200 触发 EmptyResponseError）：拦截器
已截到 200 原始块时仍写 0600 证据并 exit 11
（reason="exception_after_200:<ExcType>"），只有完全没有 200 截获的
连接类异常维持 exit 1 无证据。方向安全：任何窄盲区都不可能假阳 exit 0。

用法（当前全局已用≥60，必须带 --beyond-normal）：
  python -m orchestrator.gateway.quota_ledger approve --batch-id \\
      T6-B8-probejson --task 6 --stage coding_debug \\
      --beyond-normal --approved 1 --note "..."
  SIMPRAXIS_ALLOW_PAID=1 python scripts/phase2/diag_json_mode.py \\
      --batch-id T6-B8-probejson [--max-tokens 32768] \\
      [--thread t6-genesis-ascent-20261016a] [--out PATH]
"""
from __future__ import annotations

import argparse
import ast
import datetime
import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_text,
    ensure_private_dir,
)
from orchestrator.common.runtime_paths import phase2_runs_root  # noqa: E402
from orchestrator.common.secrets import redact_text  # noqa: E402
from orchestrator.common.validators import validate_batch_id, validate_thread_id  # noqa: E402
from orchestrator.gateway.quota_ledger import (  # noqa: E402
    QuotaLedger,
    billing_context,
)
from orchestrator.gateway.router import (  # noqa: E402
    NonRetryableError,
    ModelGateway,
)

STAGE = "coding_debug"          # 硬规则 1：探针只诊断文本 coding stage
TASK = 6
DEFAULT_THREAD = "t6-genesis-ascent-20261016a"
HEAD_TAIL_CHARS = 300
TIMEOUT_S = 900
SINGLE_ATTEMPT = 1             # 单发：一次运行至多一次付费 HTTP
DEFAULT_MAX_TOKENS = 32768
MIN_PROBE_MAX_TOKENS = 16385   # 与既有 32k 上限探针同口径
MAX_PROBE_MAX_TOKENS = 65536
TARGET_PATH = "genesis_humanoid_dr.py"
# 压力 code 长度窗（与 _probe_prompt 指令一致；下限门控 enforced，
# 超窗只作证据标志，见 _verify_envelope）。
PROBE_CODE_MIN_CHARS = 4000
PROBE_CODE_MAX_CHARS = 8000

# A 段转换探针的固定入参：仅喂给 litellm 客户端 map_openai_params 取证，
# 绝不传给 ModelGateway（网关模型 ID 只读 stage_models.yaml，硬规则 1）。
_TRANSFORM_PROBE_MODEL = "deepseek-v4.1-flash"

# 最小合法 JSON Schema：顶层 {"chunk": {...}}，不设 additionalProperties
# 之类可能被 Anthropic 工具 schema 过滤的花样。模块级常量，单测直接钉结构。
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["chunk"],
    "properties": {
        "chunk": {
            "type": "object",
            "required": ["path", "index", "total", "code"],
            "properties": {
                "path": {"type": "string"},
                "index": {"type": "integer"},
                "total": {"type": "integer"},
                "code": {"type": "string"},
            },
        },
    },
}
JSON_SCHEMA_RF = {
    "type": "json_schema",
    "json_schema": {"name": "chunk_envelope", "schema": RESPONSE_SCHEMA},
}
# json_schema 经 litellm 翻成强制 tool call 时的约定工具名（A 段静态取证
# map_openai_params 实测来源）；exit 0 合取要求原始 tool_use 块名与此逐字
# 相等。脚本内单一源，禁止第二个字面常量。
EXPECTED_TOOL_NAME = "json_tool_call"

_URL_RE = re.compile(r"https?://[^\s\"'）)]+")

EXIT_GUARD = 2
EXIT_REJECTED = 10
EXIT_ERROR = 1
EXIT_ACCEPTED_NOT_ENFORCED = 11   # HTTP 200 但强制力核验未过 → sentinel 分支信号


def _scrub(text: str, limit: int | None = None) -> str:
    """证据脱敏：密钥正则单一源 + URL 结构化剥离（异常串可能带 api_base）。"""
    out = _URL_RE.sub("[url]", redact_text(text))
    return out if limit is None else out[:limit]


# ---------------------------------------------------------------------------
# A 段：litellm 客户端 transform 静态取证（纯函数，零触网、可重复）
# ---------------------------------------------------------------------------

def _map_one_shape(config: object, response_format: dict) -> dict:
    """对单个 response_format 形态跑 map_openai_params，取证 wire 参数键。"""
    try:
        wire = config.map_openai_params(
            non_default_params={
                "max_tokens": DEFAULT_MAX_TOKENS,
                "temperature": 0,
                "response_format": response_format,
            },
            optional_params={},
            model=_TRANSFORM_PROBE_MODEL,
            drop_params=True,
        )
        return {
            "param_keys": sorted(wire.keys()),
            "wire_has_tools": "tools" in wire,
            "wire_has_tool_choice": "tool_choice" in wire,
            "wire_has_output_format": "output_format" in wire,
            # 键不存在给 None（json_object 形态即被静默丢弃、json_mode 不置）。
            "json_mode": wire["json_mode"] if "json_mode" in wire else None,
        }
    except Exception as exc:  # noqa: BLE001 —— 转换层异常如实取证，不吞成假成功
        return {"raised": f"{type(exc).__name__}: {_scrub(str(exc), 200)}"}


def _static_transform_shapes() -> dict:
    """零触网取证两形态经 litellm anthropic 转换后的 wire 参数形状。

    导入顺序坑：裸 import litellm 在 Python 3.10 会 ImportError(NotRequired)；
    先经生产链 import orchestrator.gateway.router 再导入 litellm 子模块。
    """
    result: dict = {
        "litellm": None,
        "model": _TRANSFORM_PROBE_MODEL,
        "shapes": {},
    }
    try:
        import orchestrator.gateway.router  # noqa: F401  typing 垫片先行
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig
        result["litellm"] = importlib.metadata.version("litellm")
    except Exception as exc:  # noqa: BLE001 —— 导入失败两形态都记 raised
        raised = {"raised": f"{type(exc).__name__}: {_scrub(str(exc), 200)}"}
        result["shapes"] = {"json_object": dict(raised),
                            "json_schema": dict(raised)}
        return result
    config = AnthropicConfig()
    result["shapes"]["json_object"] = _map_one_shape(
        config, {"type": "json_object"})
    result["shapes"]["json_schema"] = _map_one_shape(
        config, JSON_SCHEMA_RF)
    return result


# ---------------------------------------------------------------------------
# B 段：transport 闭包——在 litellm.completion 单点注入 json_schema 形态
# ---------------------------------------------------------------------------

# monkey-patch 靶点（红线测试 AST 钉死此字面：只允许同步类 post，
# 不得误包异步 post 路径）。该字面同时是类解析的唯一来源。
HTTPHANDLER_POST_TARGET = \
    "litellm.llms.custom_httpx.http_handler.HTTPHandler.post"

_MISSING = object()


def _resolve_handler_class():
    """从 HTTPHANDLER_POST_TARGET 字面解析同步 HTTPHandler 类。

    靶点形如 "<module-path>.HTTPHandler.post"；本探针只允许替换该同步类
    的 post，解析不出/后缀不对立即失败（宁停勿错包到 async 路径）。
    """
    head, sep, method = HTTPHANDLER_POST_TARGET.rpartition(".")
    if not sep or method != "post":
        raise RuntimeError(f"非法 HTTPHandler 靶点：{HTTPHANDLER_POST_TARGET!r}")
    module_path, sep, class_name = head.rpartition(".")
    if not sep or class_name != "HTTPHandler":
        raise RuntimeError(f"非法 HTTPHandler 靶点：{HTTPHANDLER_POST_TARGET!r}")
    return getattr(importlib.import_module(module_path), class_name)


def _choice_level_meta(resp: object) -> dict:
    """litellm 转换后 choice 级三字段——**仅作"已剥离"对照证据**。

    1.99.4 非流式 json_mode 路径（transformation.py L2487-2499 强制
    stop_reason="stop"，L2585-2607 tool_use 重序列化成 Message(content=str)
    不带 tool_calls）下，真 tool 交付与模型自觉吐 JSON 在这三字段字面相同
    （False/[]/"stop"），不能据此分辨；真正取证在 HTTPHandler 原始响应层。
    全 getattr 防御，任何缺失落 None，绝不影响 resp 原样返回。
    """
    meta = {"tool_calls_present": None, "tool_call_names": None,
            "finish_reason": None}
    try:
        choices = getattr(resp, "choices", None)
        first = choices[0] if choices else None
        message = getattr(first, "message", None)
        if message is None:
            return meta
        name_set: set[str] = set()
        tool_calls = getattr(message, "tool_calls", None)
        meta["tool_calls_present"] = (
            bool(tool_calls) if isinstance(tool_calls, list) else False)
        if isinstance(tool_calls, list):
            for call in tool_calls:
                func = getattr(call, "function", None)
                name = getattr(func, "name", None) if func is not None else None
                if not isinstance(name, str):
                    name = getattr(call, "name", None)
                if isinstance(name, str):
                    name_set.add(name)
        meta["tool_call_names"] = sorted(name_set)
        fr = getattr(first, "finish_reason", None)
        meta["finish_reason"] = fr if isinstance(fr, str) else None
    except Exception:  # noqa: BLE001 —— 元信息提取永不影响主响应返回
        return {"tool_calls_present": None, "tool_call_names": None,
                "finish_reason": None}
    return meta


def _request_booleans(post_kwargs: dict) -> dict:
    """从 post 调用 kwargs 只取请求侧四布尔/名称（绝不存请求体——prompt
    含压力码）。anthropic 同步路径以 data=序列化串下发；json= dict 形态
    同样防御支持。解析不出给 None。"""
    none4 = {"wire_req_has_tools": None, "wire_req_tool_names": None,
             "wire_req_tool_choice": None,
             "wire_req_has_response_format": None}
    body = post_kwargs.get("json")
    if not isinstance(body, dict):
        data = post_kwargs.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "ignore")
        if isinstance(data, str):
            try:
                body = json.loads(data)
            except (ValueError, TypeError):
                body = None
    if not isinstance(body, dict):
        return none4
    tools = body.get("tools")
    names: set[str] = set()
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                names.add(tool["name"])
    return {
        "wire_req_has_tools": bool(tools),
        "wire_req_tool_names": sorted(names),
        "wire_req_tool_choice": bool(body.get("tool_choice")),
        "wire_req_has_response_format": "response_format" in body,
    }


class _PostInterceptor:
    """单次 completion() 期间临时替换 HTTPHandler.post 类属性。

    只从方舟原始 httpx.Response 提取最小元数据（状态码/块类型/tool 名），
    绝不复制 tool_use 的 input、块 text 或任何 code 原文。litellm 内部
    重试产生多次 post 时逐条记 attempts；同步非流式路径 post 严格串行，
    最后一个未抛异常的 post 即最终 transform 的响应，final 标记随之移动。
    install/restore 必须配对（闭包 finally 保证），不可重入；异常原样
    抛出不吞，异常对象带 response（4xx/5xx）时也记一条 status。
    """

    def __init__(self, handler_cls):
        self._cls = handler_cls
        self._orig = None
        self._installed = False
        self.attempts: list[dict] = []
        self.request_meta: "dict | None" = None
        self._n = 0

    def install(self) -> "_PostInterceptor":
        assert not self._installed, "HTTPHandler.post 拦截不可重入"
        orig_post = self._cls.post          # 类上取到的是裸函数（非 bound method）
        interceptor = self

        def wrapper(instance, *args, **kwargs):
            # 嵌套函数挂到类上走正常 descriptor 绑定：instance 是 HTTPHandler
            # 实例；interceptor 经闭包捕获。同步非流式路径 post 严格串行，
            # 最后一个未抛异常的 post 返回值即 litellm 最终 transform 的响应。
            interceptor._n += 1
            n = interceptor._n
            if interceptor.request_meta is None:
                try:
                    interceptor.request_meta = _request_booleans(kwargs)
                except Exception:  # noqa: BLE001 —— 取证不影响请求
                    interceptor.request_meta = {
                        "wire_req_has_tools": None,
                        "wire_req_tool_names": None,
                        "wire_req_tool_choice": None,
                        "wire_req_has_response_format": None}
            try:
                resp = orig_post(instance, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 不吞异常，仅补记 status
                err_resp = getattr(exc, "response", None)
                if err_resp is not None:
                    item = interceptor._summarize_response(err_resp, n)
                    item["raised"] = type(exc).__name__
                    item["final"] = False
                    interceptor.attempts.append(item)
                raise
            # 后发的成功响应覆盖 final 标记（串行最后一次即最终返回）。
            for prior in interceptor.attempts:
                prior["final"] = False
            item = interceptor._summarize_response(resp, n)
            item["final"] = True
            interceptor.attempts.append(item)
            return resp

        self._orig = orig_post
        self._wrapper = wrapper
        self._cls.post = wrapper
        self._installed = True
        return self

    def restore(self) -> None:
        if self._installed:
            self._cls.post = self._orig
            self._installed = False

    @staticmethod
    def _summarize_response(resp: object, n: int) -> dict:
        """单个 httpx.Response → 最小元数据；非 200/坏 JSON/异形只记状态。

        绝不读取/复制 tool_use 的 input 内容（生产里即 code 原文）：只判
        input 是否 is None；text 块的 text 字段也不碰。
        """
        item = {"n": n, "http_status": None, "parse_ok": False,
                "tool_channel": False, "tool_input_null": False,
                "blocks": []}
        status = getattr(resp, "status_code", None)
        item["http_status"] = status if isinstance(status, int) else None
        # 全包防御：wrapper 自身永远不得抛非原始异常（非 200/坏 JSON/异形
        # 响应一律只落已有最小字段）。
        try:
            body = resp.json()
            if isinstance(body, dict):
                item["parse_ok"] = True
                raw_blocks = body.get("content")
                if isinstance(raw_blocks, list):
                    for block in raw_blocks:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        btype = btype if isinstance(btype, str) else None
                        name = None
                        if btype == "tool_use":
                            item["tool_channel"] = True
                            bname = block.get("name")
                            if isinstance(bname, str):
                                name = bname
                            # 只判 input 是否为 null；绝不读/拷贝其内容。
                            if block.get("input", _MISSING) is None:
                                item["tool_input_null"] = True
                        item["blocks"].append(
                            {"type": btype, "name": name})
        except Exception:  # noqa: BLE001 —— 见上：取证永不炸请求
            pass
        return item

    def captured_200(self) -> bool:
        """是否截到过任一 HTTP 200（用于异常路径 exit 1 vs 11 分流）。"""
        return any(a.get("http_status") == 200 for a in self.attempts)

    def evidence(self, litellm_resp: "object | None" = None) -> dict:
        """组装证据 wire_response 段（attempts 原样带出，含 final 标记）。

        顶层摘要取 final 尝试（串行最后成功 post）；无 final 时取最后一条
        （如只截到 4xx 抛错）或全 None。choice 级三字段附最终 ModelResponse。
        """
        attempts = [dict(a) for a in self.attempts]
        final_item = next((a for a in attempts if a.get("final")), None)
        if final_item is None and attempts:
            final_item = attempts[-1]
        summary = {"http_status": None, "parse_ok": None,
                   "tool_channel": None, "tool_input_null": None,
                   "blocks": None}
        if final_item is not None:
            for key in summary:
                summary[key] = final_item.get(key)
        summary["choice_level"] = (
            _choice_level_meta(litellm_resp)
            if litellm_resp is not None else None)
        summary["attempts"] = attempts
        return summary


def _json_schema_transport(completion=None):
    """返回注入 response_format 并拦截 HTTPHandler.post 的 completion 闭包。

    网关（router L789-797）固定 kwargs 为 model/api_base/api_key/messages/
    max_tokens/temperature/timeout，永不含 response_format；闭包入口断言
    防误注入（若未来网关自带该参数，此处立即失败而不是静默覆盖）。
    response_format 仍在调 completion 前单点注入；post wrapper 只负责读。
    闭包属性供证据：sent_keys（实际 wire kwargs 键名）、last_meta
    （wire_request/wire_response 两段，初始 None）。
    """
    import orchestrator.gateway.router  # noqa: F401  typing 垫片先行
    if completion is None:
        from litellm import completion
    # 靶点字面解析（红线钉死同步类；import 顺序坑由上面的 router 先行解决）。
    # 局部变量避开裸类名（源码红线：类名只允许出现在靶点字面串里）。
    _handler_cls = _resolve_handler_class()

    def _send(**kwargs):
        assert "response_format" not in kwargs, (
            "transport 仅允许在此单点注入 response_format，上游不得预置")
        kwargs["response_format"] = JSON_SCHEMA_RF
        _send.sent_keys = sorted(kwargs.keys())
        interceptor = _PostInterceptor(_handler_cls).install()
        try:
            resp = completion(**kwargs)
        except Exception:
            # 异常路径也落元数据（含 200 后异常情形），restore 必须执行。
            _send.last_meta = {
                "wire_request": interceptor.request_meta,
                "wire_response": interceptor.evidence(None)}
            _send.captured_200 = interceptor.captured_200()
            raise
        finally:
            interceptor.restore()
        _send.last_meta = {
            "wire_request": interceptor.request_meta,
            "wire_response": interceptor.evidence(resp)}
        _send.captured_200 = interceptor.captured_200()
        return resp

    _send.sent_keys = []
    _send.last_meta = None
    _send.captured_200 = False
    return _send


def _probe_prompt() -> str:
    """生产拆块信封 + run1–run3 转义压力的单发指令（user 单段纯文本）。"""
    lines = [
        "你正在参与一个 JSON 严格交付能力测试。只输出一个 JSON 对象，"
        "JSON 对象之外不得有任何文字、解释或 markdown 围栏。",
        "",
        "输出对象形状（<尖括号>为占位说明，不要输出尖括号、不要照抄本示例）：",
        '{"chunk":{"path":"' + TARGET_PATH
        + '","index":3,"total":4,"code":"<Python 源码字符串>"}}',
        "",
        "字段要求：",
        f'- path 逐字等于 "{TARGET_PATH}"；index 是整数 3；total 是整数 4；',
        "- code 是一个 4000 至 8000 字符的合法 Python 3 模块源码字符串，"
        "定义若干函数使长度达标，并必须同时包含：",
        r'  1. Windows 风格路径字符串，例如 r"C:\Users\bot\new\task"，'
        r"或含 \n、\t 转义序列的普通字符串；",
        r"  2. 正则表达式字符串，含 \d、\b 等反斜杠序列；",
        '  3. 三引号 docstring（内含换行与引号）；同时使用单引号与双引号'
        "字符串；含中文注释。",
        "",
        "JSON 转义纪律（本测试重点）：code 内所有换行必须转义为 \\n、"
        "回车为 \\r、制表符为 \\t、反斜杠转义为 \\\\、字符串内双引号转义为 "
        '\\"；输出字节流中禁止出现任何裸控制字符（裸换行/裸回车/裸制表符），'
        "禁止字符串内出现未转义的双引号。除这一个 JSON 对象外不要输出任何内容。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 回包四层严格核验（任何异常不得炸出函数——付费段之后，崩了等于白烧 seq）
# ---------------------------------------------------------------------------

def _verify_envelope(raw_text: str) -> dict:
    """json.loads(strict) + 信封字段 + code 可解析 + 五个压力 marker。"""
    result = {
        "json_ok": False,
        "json_error": None,
        "envelope_ok": False,
        "path_ok": False,
        "index_ok": False,
        "total_ok": False,
        "code_present": False,
        "code_python_ok": False,
        "code_python_error": None,
        "code_chars": 0,
        # MINOR3：超窗只作证据标志，不门控 enforced——长度下限防碎码逃压力，
        # 超窗并不能伪造"强制合法 JSON"（生产机器口径也只有上限守卫 26000）。
        "code_over_max": False,
        "has_backslash": False,
        "has_double_quote": False,
        "has_single_quote": False,
        "has_triple_quote": False,
        "has_non_ascii": False,
        "stress_all": False,
        "enforced": False,
    }
    try:
        # strict 默认（不给 strict=False）：字符串内裸控制字符直接失败，
        # 这正是 run1–run3 事故（char 18604 未转义分隔符、char 139 裸控字符）。
        obj = json.loads(raw_text)
        result["json_ok"] = True
    except json.JSONDecodeError as exc:
        result["json_error"] = _scrub(
            f"Char {exc.pos}: {exc.msg} (line {exc.lineno} col {exc.colno})",
            200)
        return result
    except Exception as exc:  # noqa: BLE001 —— 其他解析异常同样按 json 失败记录
        result["json_error"] = _scrub(f"{type(exc).__name__}: {exc}", 200)
        return result
    try:
        if not isinstance(obj, dict):
            return result
        chunk = obj.get("chunk")
        if not isinstance(chunk, dict):
            return result
        code = chunk.get("code")
        result["code_present"] = isinstance(code, str)
        result["path_ok"] = chunk.get("path") == TARGET_PATH
        index = chunk.get("index")
        total = chunk.get("total")
        # type(...) is int：bool 是 int 子类，不许冒充信封整数字段。
        result["index_ok"] = type(index) is int and index == 3
        result["total_ok"] = type(total) is int and total == 4
        result["envelope_ok"] = (
            result["path_ok"] and result["index_ok"] and result["total_ok"]
            and result["code_present"])
        if not result["code_present"]:
            return result
        result["code_chars"] = len(code)
        result["code_over_max"] = len(code) > PROBE_CODE_MAX_CHARS
        result["has_backslash"] = "\\" in code
        result["has_double_quote"] = '"' in code
        result["has_single_quote"] = "'" in code
        result["has_triple_quote"] = '"""' in code or "'''" in code
        result["has_non_ascii"] = any(ord(ch) > 127 for ch in code)
        try:
            ast.parse(code)
            result["code_python_ok"] = True
        except SyntaxError as exc:
            msg = exc.msg or "SyntaxError"
            if exc.lineno is not None:
                msg = f"{msg} (line {exc.lineno})"
            result["code_python_error"] = _scrub(msg, 200)
        result["stress_all"] = (
            result["has_backslash"] and result["has_double_quote"]
            and result["has_single_quote"] and result["has_triple_quote"]
            and result["has_non_ascii"]
            and result["code_chars"] >= PROBE_CODE_MIN_CHARS)
        result["enforced"] = (
            result["json_ok"] and result["envelope_ok"]
            and result["code_python_ok"] and result["stress_all"])
    except Exception as exc:  # noqa: BLE001 —— 核验自身任何意外都不得炸出
        result["code_python_error"] = _scrub(
            f"{type(exc).__name__}: {exc}", 200)
    return result


def _classify_not_enforced(verify: dict, wire_response: "dict | None") -> str:
    """200 已接受但合取不成立时的 exit 11 reason（四类，见模块 docstring）。

    方向安全：任何无法确证 tool 通道的情形都不落 tool_channel_delivered。
    """
    wr = wire_response or {}
    if wr.get("tool_input_null"):
        return "tool_input_null"
    if not wr.get("parse_ok") or not wr.get("blocks"):
        return "unattestable"
    if wr.get("tool_channel"):
        # 走了 tool 通道仍内容不过：块名不符（非 EXPECTED_TOOL_NAME），或
        # tool_use 参数非法 JSON 被 litellm 以字符串透传（_convert_tool_response
        # _to_message 的 JSONDecodeError 支）。
        return "content_failed_enforcement"
    # 可解析且有块但无 tool_use：模型走纯文本通道自觉交付。
    return ("text_compliant_voluntary" if verify.get("enforced")
            else "content_failed_enforcement")


def default_out() -> Path:
    return phase2_runs_root() / "task6_ascent" / "diag_json_mode.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diag_json_mode.py",
        description="response_format 强制合法 JSON 一次性诊断探针（显式付费守卫）")
    parser.add_argument("--batch-id", required=True,
                        help="本探针自己的配额票号（如 T6-B8-probejson）")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help=f"请求 max_tokens（默认 {DEFAULT_MAX_TOKENS}，"
                             f"区间 [{MIN_PROBE_MAX_TOKENS},{MAX_PROBE_MAX_TOKENS}]）")
    parser.add_argument("--thread", default=DEFAULT_THREAD,
                        help=f"记账线程（默认 {DEFAULT_THREAD}）")
    parser.add_argument("--out", default=None,
                        help="证据 JSON 输出路径（默认 runs/task6_ascent/"
                             "diag_json_mode.json）；警告：父目录会被"
                             "强制 chmod 0700，勿指共享目录")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    # 首行付费守卫：裸跑零触网（与 paid pytest 同一环境变量）。
    if os.environ.get("SIMPRAXIS_ALLOW_PAID") != "1":
        print("diag_json_mode: 拒绝运行——本探针发起真实付费调用，"
              "必须显式设置 SIMPRAXIS_ALLOW_PAID=1（裸跑守卫，零触网 exit 2）",
              file=sys.stderr)
        return EXIT_GUARD

    args = build_parser().parse_args(argv)
    try:
        validate_thread_id(args.thread)
        validate_batch_id(args.batch_id)
        if not (MIN_PROBE_MAX_TOKENS <= args.max_tokens <= MAX_PROBE_MAX_TOKENS)\
                or not isinstance(args.max_tokens, int):
            raise ValueError(
                f"max_tokens 须在 [{MIN_PROBE_MAX_TOKENS},{MAX_PROBE_MAX_TOKENS}]"
                f" 整数区间：{args.max_tokens!r}")
    except ValueError as exc:
        print(f"diag_json_mode: 参数非法：{exc}", file=sys.stderr)
        return EXIT_GUARD
    out = Path(args.out) if args.out else default_out()

    # A 段：零触网静态取证（先于网关构造；纯函数理论上不抛，兜底不阻断 B 段，
    # 但证据必须带 static 段）。
    try:
        static_shapes = _static_transform_shapes()
    except Exception as exc:  # noqa: BLE001
        static_shapes = {
            "litellm": None,
            "model": _TRANSFORM_PROBE_MODEL,
            "shapes": {},
            "raised": f"{type(exc).__name__}: {_scrub(str(exc), 200)}",
        }

    # G-13 账外构造 fail-closed；max_attempts=1：单发；transport 闭包注入
    # json_schema 形态，ledger/preflight/记账/暂停门全部走生产路径。
    transport = _json_schema_transport()
    gateway = ModelGateway(
        ledger=QuotaLedger(),
        max_attempts=SINGLE_ATTEMPT,
        transport=transport)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": _probe_prompt()}]}]
    try:
        with billing_context(task=TASK, batch_id=args.batch_id,
                             thread_id=args.thread):
            raw, usage = gateway.call(
                STAGE, messages,
                max_tokens=args.max_tokens, temperature=0, timeout=TIMEOUT_S)
    except NonRetryableError as exc:
        meta = transport.last_meta or {}
        record = {
            "probe": "diag_json_mode",
            "accepted": False,
            "rejected": True,
            "captured_at_iso": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stage": STAGE,
            "batch_id": args.batch_id,
            "thread_id": args.thread,
            "max_tokens": args.max_tokens,
            "static": static_shapes,
            # 附拦截到的状态元数据（典型 4xx：attempts 末条带 http_status）。
            "wire_request": meta.get("wire_request"),
            "wire_response": meta.get("wire_response"),
            "err": _scrub(str(exc), 300),
        }
        try:
            ensure_private_dir(out.parent)
            atomic_write_text(
                out, json.dumps(record, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n", mode=0o600)
        except OSError as write_exc:  # 证据写失败不得变 traceback（seq 已记）
            print(f"diag_json_mode: 供应侧已拒绝但证据写失败："
                  f"{type(write_exc).__name__}: {write_exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"diag_json_mode: 供应侧明确拒绝（exit 10）证据→{out}",
              file=sys.stderr)
        return EXIT_REJECTED
    except Exception as exc:  # noqa: BLE001 —— 连接/熔断/缺票/空回答/配置类：账已记
        # 已截到 200 原始响应（典型：thinking-only 空 200 → 网关
        # EmptyResponseError，或 input handling 在 transform 阶段抛错）：
        # 仍写 0600 证据并按 exit 11 报"200 后异常"；无 200（连接类）维持
        # exit 1 无 HTTP 证据。
        if getattr(transport, "captured_200", False):
            reason = f"exception_after_200:{type(exc).__name__}"
            meta = transport.last_meta or {}
            record = {
                "probe": "diag_json_mode",
                "accepted": True,
                "exception_after_200": True,
                "reason": reason,
                "captured_at_iso": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "stage": STAGE,
                "batch_id": args.batch_id,
                "thread_id": args.thread,
                "max_tokens": args.max_tokens,
                "static": static_shapes,
                "wire_request": meta.get("wire_request"),
                "wire_response": meta.get("wire_response"),
                "err": _scrub(str(exc), 300),
            }
            try:
                ensure_private_dir(out.parent)
                atomic_write_text(
                    out, json.dumps(record, ensure_ascii=False, indent=2,
                                    sort_keys=True) + "\n", mode=0o600)
            except OSError as write_exc:
                print(f"diag_json_mode: 200 后异常且证据写失败："
                      f"{type(write_exc).__name__}: {write_exc}",
                      file=sys.stderr)
                return EXIT_ERROR
            print(f"diag_json_mode: 200 已截获但 completion 抛异常 "
                  f"{type(exc).__name__}（exit 11 reason={reason}）证据→{out}",
                  file=sys.stderr)
            return EXIT_ACCEPTED_NOT_ENFORCED
        print(f"diag_json_mode: 探针停止（无 HTTP 接受证据，原因看台账）："
              f"{type(exc).__name__}: {_scrub(str(exc), 300)}", file=sys.stderr)
        return EXIT_ERROR

    tokens_in, tokens_out = usage
    raw_text = raw if isinstance(raw, str) else str(raw)
    verify = _verify_envelope(raw_text)
    meta = transport.last_meta or {}
    wire_request = meta.get("wire_request")
    wire_response = meta.get("wire_response")
    # exit 0 合取：内容四层全过 **且** HTTPHandler 原始块证实 tool_use 通道
    # （input 非 null、块名恰为 EXPECTED_TOOL_NAME）。名不符不新增 reason
    # 字符串——tool_channel 为真即落 classifier 的 content_failed_enforcement。
    # n=1：直接单发证据，支持接缝决策，非数学证明强制力。
    blocks = wire_response.get("blocks") if wire_response else None
    expected_name_hit = isinstance(blocks, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        and b.get("name") == EXPECTED_TOOL_NAME for b in blocks)
    tool_delivered = bool(
        wire_response and wire_response.get("tool_channel")
        and not wire_response.get("tool_input_null") and expected_name_hit)
    if verify["enforced"] and tool_delivered:
        reason = "tool_channel_delivered_n1"
    else:
        reason = _classify_not_enforced(verify, wire_response)
    record = {
        "probe": "diag_json_mode",
        "accepted": True,
        "reason": reason,
        "captured_at_iso": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stage": STAGE,
        "batch_id": args.batch_id,
        "thread_id": args.thread,
        "max_tokens": args.max_tokens,
        "static": static_shapes,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        "raw_chars": len(raw_text),
        "raw_head": _scrub(raw_text[:HEAD_TAIL_CHARS]),
        "raw_tail": _scrub(raw_text[-HEAD_TAIL_CHARS:]),
        # HTTPHandler.post 拦截元数据：请求侧四布尔/工具名（无请求体），
        # 响应侧块类型/tool 名/status/attempts（无 input/text/code 原文），
        # 另附 choice 级三字段作"litellm 已剥离"对照。假网关下两段均 None。
        "wire_request": wire_request,
        "wire_response": wire_response,
        # 四层核验全字段平铺（含 enforced 顶层布尔）。
        **verify,
    }
    try:
        ensure_private_dir(out.parent)
        atomic_write_text(
            out, json.dumps(record, ensure_ascii=False, indent=2,
                            sort_keys=True) + "\n", mode=0o600)
    except OSError as write_exc:  # 接受路径证据写失败也不得 traceback（seq 已记）
        print(f"diag_json_mode: 供应侧已接受但证据写失败："
              f"{type(write_exc).__name__}: {write_exc}", file=sys.stderr)
        return EXIT_ERROR
    if reason == "tool_channel_delivered_n1":
        print(f"diag_json_mode: accepted=True tool_channel=True enforced=True "
              f"reason={reason} out_tokens={tokens_out} "
              f"code_chars={verify['code_chars']} 证据→{out}")
        return 0
    # HTTP 200 但合取不成立：json_schema 接缝未获 n=1 直接支持，
    # 走 sentinel 围栏纯文本定界分支（具体原因看 reason）。
    print(f"diag_json_mode: accepted=True 合取不成立 reason={reason} "
          f"tool_channel={wire_response.get('tool_channel') if wire_response else None} "
          f"json_error={verify['json_error']} "
          f"envelope_ok={verify['envelope_ok']} "
          f"code_python_ok={verify['code_python_ok']} "
          f"stress_all={verify['stress_all']}（exit 11）证据→{out}",
          file=sys.stderr)
    return EXIT_ACCEPTED_NOT_ENFORCED


if __name__ == "__main__":
    sys.exit(main())
