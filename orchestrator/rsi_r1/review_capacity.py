"""R1.3 审查包容量测量（Plan §5「T0 固定 review 包测量方法与容量约束」）。

本模块只做**测量与拒绝**，不发网、不构造 sender、不读候选私有数据：

* 输入帽：``profiles.REVIEW_INPUT_TOKENS_R13``（20000 token，R1/R1.2 已实测
  通过的请求门）；不是候选回合输入帽，也不靠字符数估算；
* 输出帽：``REVIEW_OUTPUT_TOKENS_R13``（32768，含该端点的思考 token，承 D-10）；
* 字节帽：``REVIEW_PACKET_BYTES_R13``（120 KiB，UTF-8）；
* 任何一项超限 → ``REVIEW_PACKET_TOO_LARGE``，**付费前**停止（不静默截断、
  不临时拆成更多 POST）。

测量口径（先封存后使用，T5 用最大合法 192 run/完整源码报告压力件复验）：

1. 请求 = 1 条 system message + 1 条 user message（非 agentic 文本请求）；
2. ``tokens = len(tokenizer.encode(system).ids) + len(tokenizer.encode(user).ids)
   + FRAMING_OVERHEAD_TOKENS``，其中 overhead 覆盖消息包装与最小角色/分隔符；
3. ``bytes = len((system + "\n" + user).encode("utf-8"))``；
4. tokenizer 必须是本地 ``tokenizer.json`` 且字节 sha256 钉死（逐字节校验）。

tokenizer 文件按项目纪律只从 ``/mnt`` 读；本模块不下载任何东西。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from orchestrator.rsi_r1 import profiles as P

CAPACITY_SCHEMA = "simpraxis.rsi_r13.review_capacity.v1"

#: 消息包装/角色分隔的最小开销（保守估计）；测量口径冻结在本文档。
FRAMING_OVERHEAD_TOKENS = 16
#: 单条评审 JSON 回复的字节上限（6 维 × 有界理由/引用数量；schema 另有限定）。
REVIEW_OUTPUT_JSON_BYTES_R13 = 65_536

DEFAULT_TOKENIZER_PATH = (
    "/mnt/artifacts/simpraxis/rsi_r01/assets/tokenizers/"
    "deepseek-ai__DeepSeek-V3.1/tokenizer.json")
DEFAULT_TOKENIZER_SHA256 = (
    "32b34a41212e92f62e859cbbea121ae705a1fabbf157d9acf22d134ecd8dcf70")


class ReviewCapacityError(RuntimeError):
    """容量/配置拒绝（fail-closed）；``code`` 公开可机判。"""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_tokenizer(path: str | Path = DEFAULT_TOKENIZER_PATH,
                   sha256_hex: str = DEFAULT_TOKENIZER_SHA256) -> Any:
    """加载本地 tokenizer 并逐字节校验 sha256（不联网）。"""
    p = Path(path)
    if not p.is_file():
        raise ReviewCapacityError("TOKENIZER_ABSENT", f"tokenizer 不存在：{p}")
    got = _file_sha256(p)
    if got != sha256_hex:
        raise ReviewCapacityError(
            "TOKENIZER_SHA_MISMATCH",
            f"tokenizer sha256={got} 与冻结值 {sha256_hex} 不符")
    from tokenizers import Tokenizer  # 本地文件；不联网

    return Tokenizer.from_file(str(p))


def _count(tokenizer: Any, text: str) -> int:
    return len(tokenizer.encode(text).ids)


def measure_request(*, system: str, user: str,
                    tokenizer: Any,
                    input_tokens_cap: int = P.REVIEW_INPUT_TOKENS_R13,
                    output_tokens: int = P.REVIEW_OUTPUT_TOKENS_R13,
                    packet_bytes_cap: int = P.REVIEW_PACKET_BYTES_R13,
                    label: str = "") -> "dict[str, Any]":
    """测量一份拟发审查请求；超限即 ``REVIEW_PACKET_TOO_LARGE``。

    返回容量事实（可逐字节留档）；不包含任何请求正文（避免正文进日志）。
    """
    if not isinstance(system, str) or not isinstance(user, str):
        raise ReviewCapacityError("REQUEST_INVALID", "system/user 必须是字符串")
    system_tokens = _count(tokenizer, system)
    user_tokens = _count(tokenizer, user)
    tokens = system_tokens + user_tokens + FRAMING_OVERHEAD_TOKENS
    body = (system + "\n" + user).encode("utf-8")
    facts = {
        "schema": CAPACITY_SCHEMA,
        "schema_version": P.SCHEMA_VERSION,
        "label": label,
        "method": {
            "tokens": "len(encode(system)) + len(encode(user)) + framing_overhead",
            "framing_overhead_tokens": FRAMING_OVERHEAD_TOKENS,
            "bytes": "len((system + '\\n' + user).encode('utf-8'))",
        },
        "system_tokens": system_tokens,
        "user_tokens": user_tokens,
        "input_tokens": tokens,
        "input_tokens_cap": int(input_tokens_cap),
        "headroom_tokens": int(input_tokens_cap) - tokens,
        "bytes": len(body),
        "packet_bytes_cap": int(packet_bytes_cap),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "output_tokens": int(output_tokens),
        "output_json_bytes_cap": REVIEW_OUTPUT_JSON_BYTES_R13,
    }
    if len(body) > int(packet_bytes_cap):
        raise ReviewCapacityError(
            "REVIEW_PACKET_TOO_LARGE",
            f"{label or 'packet'} 字节 {len(body)} > 帽 {packet_bytes_cap}")
    if tokens > int(input_tokens_cap):
        raise ReviewCapacityError(
            "REVIEW_PACKET_TOO_LARGE",
            f"{label or 'packet'} 输入 token {tokens} > 帽 {input_tokens_cap}")
    return facts


def assert_review_output_bytes(text: str) -> int:
    """审查回复 JSON 的字节上限（格式失败占预算，不补呼）。"""
    size = len(str(text).encode("utf-8"))
    if size > REVIEW_OUTPUT_JSON_BYTES_R13:
        raise ReviewCapacityError(
            "REVIEW_OUTPUT_TOO_LARGE",
            f"回复 {size} B > 帽 {REVIEW_OUTPUT_JSON_BYTES_R13}")
    return size


def registered_caps(profile: P.CampaignProfile) -> "dict[str, Any]":
    """从 profile 读 review 专用帽（单一真值；不允许消费者另写字面量）。"""
    if not profile.is_r13:
        raise ReviewCapacityError("PROFILE_NOT_ELIGIBLE",
                                  f"review 容量只服务 R1.3，实为 {profile.profile_id}")
    extra = profile.extra
    return {
        "schema": CAPACITY_SCHEMA,
        "input_tokens": int(extra["review_input_tokens"]),
        "output_tokens": int(extra["review_output_tokens"]),
        "packet_bytes": int(extra["review_packet_bytes"]),
        "output_json_bytes": REVIEW_OUTPUT_JSON_BYTES_R13,
        "framing_overhead_tokens": FRAMING_OVERHEAD_TOKENS,
        "tokenizer": {
            "path": DEFAULT_TOKENIZER_PATH,
            "sha256": DEFAULT_TOKENIZER_SHA256,
        },
    }
