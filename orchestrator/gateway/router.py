#!/usr/bin/env python3
"""SimPraxis 统一模型网关（火山方舟 Anthropic Messages 兼容端点 /api/coding）。

三阶段（orchestrator/gateway/stage_models.yaml 是仓库内唯一允许写模型 ID 的地方；
供应变化时 v4.2 决策 3：执行者必须先 STOP→inventory 清点→请示用户，获批后才改该文件
model_id 并跑 --validate-models，禁止自行换 ID/换供应商/猜 ID，禁止把 ID 写进代码）：
  macro_ideation  方向探索与可行性调研（默认 deepseek-v4.1-flash）
  coding_debug    环境适配与代码编写（默认 glm-5.3-flash）
  visual_rollout  闭环仿真评估与视频审阅（默认 ark-code-latest，视觉 2026-10-12 实证）

凭据（scripts/00_env.sh 已做形状校验）：SIMPRAXIS_LLM_BASE_URL / SIMPRAXIS_LLM_TOKEN，
未导出时回退读 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN；Bearer 与 x-api-key 两种
鉴权均已实证 200，本文件运行期用 Bearer。

CLI 退出码（--validate-models）：0=三 stage 全绿；2=不可重试错误/配置错误/视觉断言
失败/重试耗尽（m2：含构造器失败，stderr 单行，不裸 traceback）；3=配额长熔断窗口内
（PAUSED_WAITING_QUOTA，--ignore-pause 人工恢复）。失败时证据落新时间戳目录
FAILURE.json 且 latest 不移动（m4）。

失败语义：
- 瞬时限流（429）/5xx/连接错误：先尊重 Retry-After（响应头/错误体秒数，两传输层一致；
  v4.1 m3），无该头再指数退避；
- 配额耗尽（m9：优先判 Messages 信封 error.type/error.code——type/code 明示
  rate_limit 的即使 message 含 quota 字样也按瞬时限流；无结构字段才退回全文正则
  匹配 quota/balance/欠费/余额）：立即写长熔断状态并抛 GatewayPausedError；退避预算
  耗尽同样转长熔断（默认 10 分钟）；
- 404/400/model-not-found/回显名漂移：立即抛 NonRetryableError，消息必须给 SWAP_HINT
  请示指引（v4.2 决策 3：STOP→inventory 清点（日期版本如 glm-5-3-flash-260828 仅作
  建议）→报用户批准后才改 stage_models.yaml 的 model_id 并重跑 --validate-models；
  获批前禁止改 YAML、禁止换供应商、禁止猜 ID；重验批次预算外另计、逐次记录）；
- 401/403：立即失败零重试，不与配额混淆；400 同样不可重试；
- model_id 为 null 的 stage：构造期要求 open_question 锚点（逐字在
  .omc/plans/open-questions.md），调用抛 StageUnresolvedError，不触网；
  --validate-models 对其报告 SKIP-DUE-TO-NULL。
- 视觉：Ark 实测最小边 14px（8px 报 InvalidParameter: Image dimensions are too
  small）；校验图为 stdlib 现场生成的 32x32 纯色 PNG，不引用任何外部资产。

两条传输路径共用同一退避/熔断分类层：
- 运行期 call()：litellm completion，model="anthropic/<id>"（Phase 1 LangGraph 用）；
- --validate-models：requests 直 POST /v1/messages，以便如实记录 http_status 与
  响应 type=message（litellm 标准化后不透出这两个字段）。
若 litellm anthropic/ provider 对 Ark 非 Claude 浮动别名不兼容（§18 开放问题），
允许把运行期路径也切到直 POST，但必须复用本文件同一分类/退避/熔断层，断言不放宽。

状态文件：$SIMPRAXIS_GATEWAY_STATE/quota_pause.json（0600）。日志经脱敏过滤器，
绝不输出 token 或认证头。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Callable

import litellm
import requests
import yaml
from litellm.exceptions import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout as LiteLLMTimeout,
)

litellm.drop_params = True
litellm.suppress_debug_info = True

STAGE_MODELS_PATH = Path(__file__).with_name("stage_models.yaml")
STAGES = ("macro_ideation", "coding_debug", "visual_rollout")
VISION_STAGE = "visual_rollout"
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding"
ANTHROPIC_VERSION = "2023-06-01"
PAUSE_FILENAME = "quota_pause.json"
VALIDATE_DIRNAME = "model_validate"
MIN_IMAGE_EDGE = 14  # Ark 实测：8px 报 InvalidParameter，14px 通过（2026-10-12）
REPO_ROOT = Path(__file__).resolve().parents[2]

# 404/400/回显名漂移时的处置流程（v4.2 决策 3：禁止自行换 ID / 换供应商 / 现场猜 ID）
SWAP_HINT = (
    "【停止并请示用户（v4.2 决策 3）】模型 ID 被供应侧拒绝（404/400）或回显名漂移："
    "立即 STOP，先跑 python -m orchestrator.gateway.inventory_models 清点（不计费），"
    "把建议的同家族日期版本（如 glm-5-3-flash-260828）与证据（错误原文、inventory 行、"
    "回显对照）报给用户；用户明确批准前禁止改 orchestrator/gateway/stage_models.yaml 的 "
    "model_id、禁止换供应商、禁止现场猜 ID。获批后只改该文件对应 stage 的 model_id，"
    "并重跑 python -m orchestrator.gateway.router --validate-models；"
    "换供重验批次在计划 8 次付费预算之外，另计并逐次记入 Milestone 记录。"
)

# v4.1 n1：IGNORECASE——BEARER/X-API-KEY/ARK- 等大写形态同样必须脱敏（脱敏方向宁可过杀）
_SECRET_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._\-]{12,}|"
    r"(x-api-key:\s*)[A-Za-z0-9._\-]{12,}|"
    r"(ark-|hf_|gh[pousr]_|nvapi-|sk-)[A-Za-z0-9._\-]{10,}",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    def _sub(m: re.Match) -> str:
        prefix = next((g for g in m.groups() if g), "")
        return f"{prefix}***REDACTED***"
    return _SECRET_RE.sub(_sub, str(text))


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


logger = logging.getLogger("simpraxis.gateway")
logger.setLevel(logging.INFO)
if not any(isinstance(f, _RedactingFilter) for f in logger.filters):
    logger.addFilter(_RedactingFilter())


class GatewayError(RuntimeError):
    """网关基类异常。"""


class NonRetryableError(GatewayError):
    """400/401/403/404 等重试无意义的错误：立即失败，与配额暂停严格区分（m12）。"""


class StageUnresolvedError(GatewayError):
    """stage model_id 为 null（未实证能力）：不发起任何网络调用。"""

    def __init__(self, stage: str, open_question: str):
        self.stage = stage
        self.open_question = open_question
        super().__init__(f"stage={stage} model_id 为 null（未实证），open_question={open_question}")


class GatewayPausedError(GatewayError):
    """配额长熔断：在 resume_at_epoch 之前调用方不应再发起请求。"""

    def __init__(self, state: dict):
        self.state = state
        super().__init__(f"GATEWAY PAUSED stage={state.get('stage')} "
                         f"until {state.get('resume_at_iso')} reason={state.get('reason')}")


# ---- 配额长熔断状态（与 v3 同契约，alias 字段改名 stage） ----

def _state_dir() -> Path:
    return Path(os.environ.get("SIMPRAXIS_GATEWAY_STATE",
                               "/mnt/artifacts/simpraxis/gateway_state"))


def _state_path() -> Path:
    return _state_dir() / PAUSE_FILENAME


def load_pause_state() -> dict | None:
    p = _state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_paused(stage: str | None = None, now: float | None = None) -> dict | None:
    state = load_pause_state()
    if not state:
        return None
    if stage and state.get("stage") not in (stage, "*"):
        return None
    now = time.time() if now is None else now
    resume = state.get("resume_at_epoch")
    if resume and now >= float(resume):
        return None
    return state


def _write_pause_state(stage: str, reason: str, wait_sec: int) -> dict:
    now = time.time()
    state = {
        "status": "PAUSED_WAITING_QUOTA",
        "stage": stage,
        "reason": redact(reason)[:500],
        "paused_at_epoch": now,
        "paused_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "resume_at_epoch": now + wait_sec,
        "resume_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                       time.localtime(now + wait_sec)),
    }
    _state_dir().mkdir(parents=True, exist_ok=True)
    tmp = _state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_state_path())
    os.chmod(_state_path(), 0o600)
    return state


def _clear_stale_pause_state() -> None:
    if is_paused() is None and _state_path().exists():
        _state_path().unlink(missing_ok=True)


_QUOTA_RE = re.compile(
    r"quota|balance|billing|欠费|余额|insufficient\s+(?:credit|balance|quota)|"
    r"resource\s*exhausted|exceed(?:ed|s)?\s+(?:your\s+)?quota|account.*exceeded",
    re.IGNORECASE,
)
_RETRY_AFTER_NUM_RE = re.compile(r"retry[ -]?after[^\d]{0,8}(\d{1,5})", re.IGNORECASE)
_TOO_SMALL_RE = re.compile(r"dimensions?\s*(?:are|is)?\s*too\s*small", re.IGNORECASE)


def _error_body(exc_or_resp: Any) -> dict | None:
    """Messages 错误体：{"type":"error","error":{"type","code","message"}}。"""
    resp = getattr(exc_or_resp, "response", None)
    if resp is None and hasattr(exc_or_resp, "text"):
        resp = exc_or_resp  # 已经是 response 对象
    text = getattr(resp, "text", None)
    if not text:
        return None
    try:
        body = json.loads(text)
        return body if isinstance(body, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _err_field(body: dict | None, key: str) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    return str(err.get(key, "")) if isinstance(err, dict) else ""


def _error_text(exc: Exception) -> str:
    parts = [str(exc)]
    body = _error_body(exc)
    if body:
        parts.extend([_err_field(body, "type"), _err_field(body, "code"),
                      _err_field(body, "message")])
    return redact(" | ".join(p for p in parts if p))


def _is_quota_text(text: str) -> bool:
    return bool(_QUOTA_RE.search(text or ""))


# m9：Messages 信封 error.type/error.code 的结构化判别词。
# type/code 明示瞬时限流时，即使 message 文本含 "quota" 字样也按瞬时限流处理——
# 供应侧常返回 type=rate_limit_exceeded + 文案 "you exceeded your quota (per minute)"，
# v4 纯文本正则会把它误判成长熔断。
_QUOTA_FIELD_RE = re.compile(
    r"quota|balance|billing|insufficient|欠费|余额|resource[_ .-]?exhausted|account.*exceeded",
    re.IGNORECASE,
)
_RATE_LIMIT_FIELD_RE = re.compile(r"rate[_ -]?limit|too[_ -]?many[_ -]?requests", re.IGNORECASE)


def _quota_decision(etype: str, ecode: str, fallback_text: str) -> bool:
    """结构化字段优先：有 type/code 时只信 type/code；都没有才退回全文正则（m9）。"""
    if etype or ecode:
        joined = f"{etype} {ecode}"
        if _QUOTA_FIELD_RE.search(joined):
            return True
        if _RATE_LIMIT_FIELD_RE.search(joined):
            return False
    return _is_quota_text(fallback_text)


def _is_quota_error(exc: Exception) -> bool:
    body = _error_body(exc)
    return _quota_decision(_err_field(body, "type"), _err_field(body, "code"),
                           _error_text(exc))


def _is_quota_http(body: dict | None, fallback_text: str) -> bool:
    """直 POST 路径的同构分类（body 已解析，fallback_text 是拼接的降级文本）。"""
    return _quota_decision(_err_field(body, "type"), _err_field(body, "code"),
                           fallback_text)


def _retry_after_from_headers(headers: Any) -> int | None:
    if not headers:
        return None
    val = None
    try:
        val = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        val = None
    if val and str(val).isdigit():
        return min(int(val), 1800)
    return None


def _retry_after_sec(exc: Exception) -> int | None:
    val = _retry_after_from_headers(getattr(getattr(exc, "response", None), "headers", None))
    if val is not None:
        return val
    m = _RETRY_AFTER_NUM_RE.search(_error_text(exc))
    return min(int(m.group(1)), 1800) if m else None


def _message_text(body: dict) -> str:
    """Messages 成功体 content 可能是 str 或 content block 列表。"""
    content = body.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _litellm_text(resp: Any) -> str:
    try:
        content = resp.choices[0].message.content or ""
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(content, list):
        content = "".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return str(content)


def _solid_png_b64(rgb: tuple[int, int, int] = (255, 0, 0), size: int = 32) -> str:
    """stdlib 生成 size×size 纯色 RGB PNG（无外部资产；视觉校验的确定性输入）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = (b"\x00" + bytes(rgb) * size) * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


def _open_questions_path() -> Path:
    return Path(os.environ.get("SIMPRAXIS_OPEN_QUESTIONS",
                               str(REPO_ROOT / ".omc" / "plans" / "open-questions.md")))


def _creds() -> tuple[str, str]:
    token = os.environ.get("SIMPRAXIS_LLM_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        raise GatewayError("SIMPRAXIS_LLM_TOKEN/ANTHROPIC_AUTH_TOKEN 未注入："
                           "先 source scripts/00_env.sh")
    base_url = (os.environ.get("SIMPRAXIS_LLM_BASE_URL")
                or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL)
    return token, base_url


class ModelGateway:
    def __init__(self,
                 stage_models_path: str | Path = STAGE_MODELS_PATH,
                 max_attempts: int = 5,
                 base_backoff: float = 2.0,
                 max_backoff: float = 60.0,
                 max_total_wait: float = 300.0,
                 quota_pause_sec: int = 1800,
                 sleep: Callable[[float], None] = time.sleep,
                 transport: Callable[..., Any] | None = None,
                 raw_transport: Callable[..., Any] | None = None):
        self.stage_models_path = Path(stage_models_path)
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_total_wait = max_total_wait
        self.quota_pause_sec = quota_pause_sec
        self._sleep = sleep
        # 测试注入点：transport=litellm.completion 替身；raw_transport=requests.post 替身
        self._transport = transport
        self._raw_transport = raw_transport
        self.config = self._load_config()

    # ---- 配置（schema + null 锚点；无任何目录成员资格断言） ----

    def _load_config(self) -> dict:
        if not self.stage_models_path.exists():
            raise GatewayError(
                f"stage models 配置不存在：{self.stage_models_path}（Task 2 应直接提交该文件）")
        data = yaml.safe_load(self.stage_models_path.read_text(encoding="utf-8")) or {}
        errors: list[str] = []
        meta = data.get("meta") or {}
        if meta.get("schema_version") != "1.0":
            errors.append('meta.schema_version 必须为 "1.0"')
        if meta.get("endpoint") != "anthropic-messages":
            errors.append('meta.endpoint 必须为 "anthropic-messages"')
        if meta.get("base_url_env") != "ANTHROPIC_BASE_URL":
            errors.append("meta.base_url_env 必须为 ANTHROPIC_BASE_URL")
        sm = data.get("stage_models")
        if not isinstance(sm, dict):
            raise GatewayError("stage_models 缺失或不是映射（三阶段配置不可解析）")
        if set(sm.keys()) != set(STAGES):
            errors.append(f"stage_models 必须恰好包含 {STAGES}，实际={sorted(sm.keys())}"
                          "（旧 planning/coding/vision/reasoning 四别名设计已作废）")
        oq_text = ""
        oq_path = _open_questions_path()
        if oq_path.exists():
            oq_text = oq_path.read_text(encoding="utf-8")
        for stage in STAGES:
            entry = sm.get(stage)
            if not isinstance(entry, dict):
                errors.append(f"{stage}：缺少 stage 条目")
                continue
            mid = entry.get("model_id", "__MISSING__")
            caps = entry.get("capabilities")
            purpose = entry.get("purpose", "")
            if isinstance(mid, str):
                if not mid.strip() or "__" in mid:
                    errors.append(f"{stage}：model_id 仍是占位符，必须填真实 ID 或显式 null")
            elif mid is not None:
                errors.append(f"{stage}：model_id 必须是字符串或 null")
            if not isinstance(caps, list) or not caps or any(
                    c not in ("text", "vision") for c in caps):
                errors.append(f"{stage}：capabilities 必须是 [text]/[text, vision] 子集且非空")
            if not isinstance(purpose, str) or not purpose.strip():
                errors.append(f"{stage}：purpose 必填（说明该阶段职责）")
            if stage == VISION_STAGE and mid is not None and "vision" not in (caps or []):
                errors.append("visual_rollout 非 null 时 capabilities 必须含 vision")
            if mid is None:
                anchor = str(entry.get("open_question", "")).strip()
                if not anchor:
                    errors.append(f"{stage}：model_id 为 null 必须同时提供 open_question 锚点")
                elif anchor not in oq_text:
                    errors.append(
                        f"{stage}：open_question 锚点必须逐字出现在 {oq_path}：{anchor[:40]}…")
        if errors:
            raise GatewayError("stage_models 校验失败：\n  - " + "\n  - ".join(errors))
        return data

    def entry(self, stage: str) -> dict:
        if stage not in STAGES:
            raise NonRetryableError(f"未知 stage {stage}，合法值：{STAGES}")
        return self.config["stage_models"][stage]

    def model_id(self, stage: str) -> str | None:
        mid = self.entry(stage).get("model_id")
        return None if mid is None else str(mid)

    def null_stages(self) -> list[str]:
        return [s for s in STAGES if self.model_id(s) is None]

    # ---- 运行期：litellm anthropic/ provider + v3 同款退避/熔断 ----

    def call(self, stage: str, messages: list[dict[str, Any]], *,
             max_tokens: int = 512, temperature: float = 0.2,
             timeout: int = 120) -> str:
        model_id = self.model_id(stage)
        if model_id is None:
            anchor = str(self.entry(stage).get("open_question", ""))
            raise StageUnresolvedError(stage, anchor)
        paused = is_paused(stage)
        if paused:
            raise GatewayPausedError(paused)
        token, base_url = _creds()
        completion = self._transport or litellm.completion

        attempt = 0
        wait_total = 0.0
        while True:
            try:
                resp = completion(
                    model=f"anthropic/{model_id}",
                    api_base=base_url,
                    api_key=token,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout,
                )
                content = _litellm_text(resp)
                usage = getattr(resp, "usage", None)
                in_tok = getattr(usage, "input_tokens", None) or \
                    getattr(usage, "prompt_tokens", "?")
                out_tok = getattr(usage, "output_tokens", None) or \
                    getattr(usage, "completion_tokens", "?")
                logger.info("chat ok stage=%s requested=%s echo=%s tokens in=%s out=%s",
                            stage, model_id, getattr(resp, "model", "?"), in_tok, out_tok)
                _clear_stale_pause_state()
                return content
            except RateLimitError as exc:
                if _is_quota_error(exc):
                    state = _write_pause_state(stage, _error_text(exc), self.quota_pause_sec)
                    logger.warning("配额类限流，进入长熔断：%s", state["resume_at_iso"])
                    raise GatewayPausedError(state) from exc
                if attempt + 1 >= self.max_attempts:
                    state = _write_pause_state(stage, "429 退避预算耗尽，转长熔断", 600)
                    raise GatewayPausedError(state) from exc
                wait = _retry_after_sec(exc)
                if wait is None:
                    wait = min(self.max_backoff,
                               self.base_backoff * (2 ** attempt)) + random.uniform(0, 0.5)
                if wait_total + wait > self.max_total_wait:
                    state = _write_pause_state(stage, "429 等待预算超限，转长熔断", 600)
                    raise GatewayPausedError(state) from exc
                logger.warning("瞬时限流 stage=%s 第 %d 次，%.1fs 后重试",
                               stage, attempt + 1, wait)
                self._sleep(wait)
                wait_total += wait
                attempt += 1
            except (ServiceUnavailableError, InternalServerError, LiteLLMTimeout,
                    APIConnectionError) as exc:
                if attempt + 1 >= self.max_attempts:
                    raise GatewayError(f"{type(exc).__name__} 重试 {self.max_attempts} 次仍失败："
                                       f"{_error_text(exc)}") from exc
                # m3：5xx/超时同样先尊重 Retry-After（响应头或错误体秒数），无头才指数退避
                wait = _retry_after_sec(exc)
                if wait is None:
                    wait = min(self.max_backoff, self.base_backoff * (2 ** attempt)) \
                        + random.uniform(0, 0.5)
                logger.warning("可重试错误 %s，%.1fs 后重试（%s）",
                               type(exc).__name__, wait, _error_text(exc)[:160])
                self._sleep(wait)
                wait_total += wait
                attempt += 1
            except NotFoundError as exc:
                # v4：404 唯一处置是换 stage_models.yaml 的 model_id，禁止重试烧预算
                raise NonRetryableError(
                    f"{type(exc).__name__} 模型 ID 被供应侧拒绝：{_error_text(exc)}\n{SWAP_HINT}"
                ) from exc
            except (AuthenticationError, PermissionDeniedError, BadRequestError) as exc:
                # m12：401/403/400 立即失败、零重试
                detail = _error_text(exc)
                if _TOO_SMALL_RE.search(detail):
                    detail += f"（Ark 图像最小边实测 {MIN_IMAGE_EDGE}px）"
                raise NonRetryableError(
                    f"{type(exc).__name__} 不可重试：{detail}") from exc
            except GatewayError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 >= 2:
                    raise GatewayError(f"未分类错误连续出现：{_error_text(exc)}") from exc
                logger.warning("未分类错误 %s，短暂重试一次", type(exc).__name__)
                self._sleep(min(self.max_backoff, self.base_backoff))
                wait_total += self.base_backoff
                attempt += 1

    # ---- 实呼校验：直 POST /v1/messages（如实记录 http_status / type=message） ----

    def _post_messages(self, stage: str, model_id: str,
                       content_blocks: list[dict], *, max_tokens: int,
                       timeout: int = 60) -> tuple[Any, dict]:
        token, base_url = _creds()
        url = base_url.rstrip("/") + "/v1/messages"
        payload = {"model": model_id, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": content_blocks}]}

        def send_one() -> Any:
            post = self._raw_transport or requests.post
            headers = {
                # 认证头只在这一行出现且永不进日志（redact 过滤器双保险）
                "Authorization": f"Bearer {token}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
            return post(url, headers=headers, json=payload, timeout=timeout)

        attempt = 0
        wait_total = 0.0
        while True:
            try:
                resp = send_one()
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt + 1 >= self.max_attempts:
                    raise GatewayError(f"连接错误重试 {self.max_attempts} 次仍失败：{exc}") from exc
                wait = min(self.max_backoff, self.base_backoff * (2 ** attempt)) \
                    + random.uniform(0, 0.5)
                self._sleep(wait)
                wait_total += wait
                attempt += 1
                continue
            status = getattr(resp, "status_code", 0)
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            if status == 200:
                return resp, body
            text = " | ".join(p for p in (
                str(body.get("message", "")) if isinstance(body, dict) else "",
                _err_field(body, "type"), _err_field(body, "code"),
                _err_field(body, "message"), getattr(resp, "text", "")) if p)
            text = redact(text)
            if status == 404 or "not_found" in _err_field(body, "code").lower() \
                    or "model-not-found" in _err_field(body, "code").lower().replace("_", "-"):
                raise NonRetryableError(f"404/model-not-found stage={stage}：{text}\n{SWAP_HINT}")
            if status in (401, 403):
                raise NonRetryableError(
                    f"HTTP {status} stage={stage}：鉴权失败，检查 ~/.bashrc 的 "
                    f"ANTHROPIC_AUTH_TOKEN 形状与归属（零重试）：{text}")
            if status == 400:
                if _TOO_SMALL_RE.search(text):
                    text += f"（Ark 图像最小边实测 {MIN_IMAGE_EDGE}px）"
                raise NonRetryableError(f"HTTP 400 stage={stage}：{text}")
            if status == 429:
                # m9：信封 type/code 优先（rate_limit_exceeded 即使文案含 quota 也按瞬时限流）
                if _is_quota_http(body, text):
                    state = _write_pause_state(stage, text, self.quota_pause_sec)
                    raise GatewayPausedError(state)
                if attempt + 1 >= self.max_attempts:
                    state = _write_pause_state(stage, "429 退避预算耗尽，转长熔断", 600)
                    raise GatewayPausedError(state)
                wait = _retry_after_from_headers(getattr(resp, "headers", None))
                if wait is None:
                    wait = min(self.max_backoff,
                               self.base_backoff * (2 ** attempt)) + random.uniform(0, 0.5)
                if wait_total + wait > self.max_total_wait:
                    state = _write_pause_state(stage, "429 等待预算超限，转长熔断", 600)
                    raise GatewayPausedError(state)
                logger.warning("瞬时限流(HTTP) stage=%s 第 %d 次，%.1fs 后重试",
                               stage, attempt + 1, wait)
                self._sleep(wait)
                wait_total += wait
                attempt += 1
                continue
            if 500 <= int(status) < 600:
                if attempt + 1 >= self.max_attempts:
                    raise GatewayError(f"HTTP {status} 重试 {self.max_attempts} 次仍失败：{text}")
                # m3：5xx 先尊重 Retry-After 响应头，无头再指数退避 + jitter
                wait = _retry_after_from_headers(getattr(resp, "headers", None))
                if wait is None:
                    wait = min(self.max_backoff, self.base_backoff * (2 ** attempt)) \
                        + random.uniform(0, 0.5)
                self._sleep(wait)
                wait_total += wait
                attempt += 1
                continue
            raise NonRetryableError(f"未分类 HTTP {status} stage={stage}：{text}")

    def ping_text(self, stage: str, *, max_tokens: int = 8) -> dict:
        model_id = self.model_id(stage)
        t0 = time.time()
        resp, body = self._post_messages(
            stage, model_id, [{"type": "text", "text": "ping，只回复 pong"}],
            max_tokens=max_tokens)
        latency_ms = int((time.time() - t0) * 1000)
        if body.get("type") != "message":
            raise NonRetryableError(
                f"stage={stage} 响应 type={body.get('type')!r}，非 message（200 但语义不对）")
        return {"stage": stage, "requested_id": model_id, "echo_model": body.get("model"),
                "http_status": resp.status_code, "type": body["type"],
                "latency_ms": latency_ms, "answer": _message_text(body)[:80]}

    def vision_check(self, *, size: int = 32, max_tokens: int = 16) -> dict:
        """对 visual_rollout 发 32x32 纯红 PNG，断言回答含 red/红（Ark 视觉实证的可复验版）。"""
        stage = VISION_STAGE
        model_id = self.model_id(stage)
        img = _solid_png_b64((255, 0, 0), size)
        blocks = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": img}},
            {"type": "text",
             "text": "What single color is this solid image? Answer with one English word."},
        ]
        t0 = time.time()
        resp, body = self._post_messages(stage, model_id, blocks, max_tokens=max_tokens)
        latency_ms = int((time.time() - t0) * 1000)
        answer = _message_text(body)
        # n2：用词边界，禁止 "tired"/"redder" 之类子串假绿
        ok = (re.search(r"\bred\b", answer.strip().lower()) is not None) or ("红" in answer)
        record = {"stage": stage, "requested_id": model_id, "echo_model": body.get("model"),
                  "http_status": resp.status_code, "latency_ms": latency_ms,
                  "image": f"{size}x{size} solid red PNG (stdlib-generated, no external asset)",
                  "min_edge_px": MIN_IMAGE_EDGE, "answer": answer[:80], "red_assert": bool(ok)}
        if not ok:
            raise GatewayError(f"视觉校验失败：32x32 纯红图回答 {answer!r} 不含 red/红")
        return record

    def validate_models(self, *, max_tokens: int = 8) -> dict:
        # 花费契约（v4.1 M2，与铁律 9/§7.3 同口径）：非 null stage 各一次文本 ping
        # （max_tokens 取 CLI 值，默认 8）；visual_rollout 额外一次视觉 ping，其输出
        # 下限固定 16 token。一批 4 个付费请求，输出上限 3*8+16=40 token。
        results: list[dict] = []
        skipped: list[str] = []

        def _base_doc(status: str) -> dict:
            return {"generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                    "endpoint": "anthropic-messages", "status": status,
                    "results": results, "skipped_null": skipped}

        try:
            for stage in STAGES:
                if self.model_id(stage) is None:
                    anchor = self.entry(stage).get("open_question", "")
                    print(f"[gateway SKIP-DUE-TO-NULL] stage={stage} open_question={anchor}")
                    skipped.append(stage)
                    continue
                rec = self.ping_text(stage, max_tokens=max_tokens)
                if stage == VISION_STAGE:
                    rec["vision"] = self.vision_check(max_tokens=max(16, max_tokens))
                    print(f"[gateway VISION OK] stage={stage} answer={rec['vision']['answer']!r}")
                results.append(rec)
                print(f"[gateway PING OK] stage={stage} requested={rec['requested_id']} "
                      f"echo={rec['echo_model']} http={rec['http_status']} "
                      f"type={rec['type']} {rec['latency_ms']}ms")
        except Exception as exc:
            # m4：失败只在新时间戳目录写 FAILURE.json（含已完成的部分结果），
            # 绝不移动 latest——latest 恒指上一次全绿证据，杜绝"最新即全绿"的误读。
            doc = _base_doc("FAIL")
            doc["error_type"] = type(exc).__name__
            doc["error"] = str(exc)[:500]
            fail_path = _write_failure_doc(doc)
            print(f"[gateway VALIDATE FAIL] 部分结果与错误已落 {fail_path}；"
                  f"latest 保持指向上一次全绿（m4）", file=sys.stderr)
            raise
        doc = _base_doc("OK")
        out_path = _write_validate_doc(doc)
        print(f"[gateway VALIDATE OK] results={len(results)} skipped={skipped} -> {out_path}")
        return doc


def _validate_root() -> Path:
    return Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                               "/mnt/artifacts/simpraxis/runs")) / VALIDATE_DIRNAME


def _write_validate_doc(doc: dict) -> Path:
    root = _validate_root()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = root / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "result.json"
    raw.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(raw, 0o600)
    latest = root / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(ts)  # 相对软链（basename 规则）
    return raw


def _write_failure_doc(doc: dict) -> Path:
    """m4：校验失败时把部分结果+错误写到新时间戳目录的 FAILURE.json。

    与 _write_validate_doc 的唯一差异：绝不触碰 latest——latest 永远指向最近一次
    全绿证据，"最新时间戳目录"不再隐含成功语义。
    """
    root = _validate_root()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = root / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "FAILURE.json"
    raw.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(raw, 0o600)
    return raw


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-models", action="store_true",
                        help="三阶段实呼校验：每 stage 一次 max_tokens=8 Messages ping，"
                             "visual_rollout 额外 32x32 视觉 ping；结果落 runs/model_validate/")
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--ignore-pause", action="store_true",
                        help="忽略仍在窗口内的 quota_pause.json（人工确认配额已恢复时使用）")
    args = parser.parse_args()
    if not args.validate_models:
        parser.error("当前仅支持 --validate-models")

    # pause 生命周期与 v3 相同：过期即清除；窗口内默认拒绝（exit 3）；--ignore-pause 人工恢复
    state = load_pause_state()
    if state is not None:
        if is_paused() is None:
            _state_path().unlink(missing_ok=True)
            print("[gateway] 检测到已过期的 quota_pause.json，已清除")
        elif args.ignore_pause:
            _state_path().unlink(missing_ok=True)
            print("[gateway] --ignore-pause：人工确认配额恢复，清除熔断状态后继续")
        else:
            print(f"[gateway VALIDATE BLOCKED] 仍在配额熔断窗口内：{state.get('resume_at_iso')}；"
                  f"确认恢复后加 --ignore-pause", file=sys.stderr)
            return 3

    # m2：构造器（读 stage_models.yaml/open-questions 锚点）同样包在 try 内——
    # 配置错误过去会裸 traceback 且退出码不确定；现在统一走退出码契约。
    try:
        gw = ModelGateway()
        gw.validate_models(max_tokens=args.max_tokens)
    except GatewayPausedError as exc:
        print(f"[gateway VALIDATE BLOCKED] {exc}", file=sys.stderr)
        return 3
    except NonRetryableError as exc:
        # 404 换模指引 / 401 鉴权 / 400 参数：立即失败，信息必须可执行
        print(f"[gateway VALIDATE FAIL] {exc}", file=sys.stderr)
        return 2
    except GatewayError as exc:
        # 配置缺失/锚点不逐字/视觉断言失败/重试耗尽等一切已知网关错误：stderr + 2，
        # 不向调用方泄漏 traceback（m2）。失败证据见新时间戳目录的 FAILURE.json。
        print(f"[gateway VALIDATE FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
