"""密钥正则与日志/证据脱敏的单一事实源（G-8/H-12）。

口径：
- ``SECRET_PATTERNS`` 同时服务路由日志 redact（router.redact）与证据包扫描
  （build_evidence_pack.scan 与测试），两处不再各持一份易漂移的正则。
- 严格模式（``redact_only=False``）= 证据包 scan_secrets 口径：形状足够具体，
  用于 FATAL 判定与回归锁定；在原 9 条 canonical（build_evidence_pack.py 注释
  「scan_secrets.sh 同款 9 条」）之上补入飞书 cli_ app_id 与 app_secret 赋值形态。
- 宽模式（``redact_only=True``）只进日志脱敏：宁可过杀，不进证据 FATAL 扫描。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

REDACTED = "***REDACTED***"


@dataclass(frozen=True)
class SecretPattern:
    name: str
    regex: str
    ignorecase: bool = False
    # 需要原样保留的前缀捕获组：redact 时这些组的拼接前缀不动，其后到匹配
    # 末尾全部替换（kv 形态会连同右引号一起吃掉，可接受——仅用于脱敏）；
    # 空元组表示整个匹配都是秘密（全替换）。
    prefix_groups: tuple[int, ...] = ()
    # True = 只用于日志宽脱敏（如 ark-xxx 短串），不参与证据包 FATAL 扫描。
    redact_only: bool = False


# 顺序即 redact 的执行顺序（链式 sub）；严格 9 条保持与历史扫描行为兼容。
SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    # --- 原 canonical 9 条（pack/scan_secrets 口径，形状从严） ---
    # 令牌类前缀入组仅为 redact 保留前缀（历史 router 口径）；scan 仍认全匹配。
    SecretPattern("hf_token", r"(hf_)[A-Za-z0-9]{20,}", prefix_groups=(1,)),
    SecretPattern("github_token", r"(gh[pousr]_)[A-Za-z0-9]{20,}",
                  prefix_groups=(1,)),
    SecretPattern("nvidia_token", r"(nvapi-)[A-Za-z0-9_-]{20,}",
                  prefix_groups=(1,)),
    SecretPattern("volc_ark_token",
                  r"(ark-)[A-Za-z0-9][A-Za-z0-9-]{12,}", prefix_groups=(1,)),
    SecretPattern("openai_like_key", r"(sk-)[A-Za-z0-9_-]{24,}",
                  prefix_groups=(1,)),
    SecretPattern("aws_access_key", r"AKIA[0-9A-F]{16}"),
    SecretPattern("slack_token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    SecretPattern("pem_private_key",
                  r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    SecretPattern(
        "assigned_secret_kv",
        # S-2：name 后允许一个可选收尾引号（组 2），覆盖 JSON/env dump 形态
        # ``"access_token" : "..."``——只收紧不放宽；值引号仍由反向引用 \5
        # 锁同引号，值字符类与长度门槛一字未松。
        r"(api[_-]?key|access[_-]?token|auth[_-]?token|authorization|"
        r"app[_-]?secret|feishu[_-]?app[_-]?secret)"
        r"([\"']?)([\s]{0,5})[:=]([\s]{0,5})(['\"])([A-Za-z0-9_./+-]{24,})\5",
        ignorecase=True, prefix_groups=(1, 2, 3, 4, 5)),
    # --- H-12 补充：飞书 cli_ 系列 app_id（证据扫描也应识别为标识型秘密） ---
    SecretPattern("lark_cli_app_id", r"cli_[a-z0-9]{16,}"),
    # --- 日志宽脱敏 sentinel（router 历史口径：前缀 + 12/10 字符即杀） ---
    SecretPattern("bearer_header", r"(Bearer\s+)[A-Za-z0-9._\-]{12,}",
                  ignorecase=True, prefix_groups=(1,), redact_only=True),
    SecretPattern("x_api_key_header", r"(x-api-key:\s*)[A-Za-z0-9._\-]{12,}",
                  ignorecase=True, prefix_groups=(1,), redact_only=True),
    SecretPattern("token_prefix_loose",
                  r"(ark-|hf_|gh[pousr]_|nvapi-|sk-)[A-Za-z0-9._\-]{10,}",
                  ignorecase=True, prefix_groups=(1,), redact_only=True),
)


def _flags(sp: SecretPattern) -> int:
    return re.IGNORECASE if sp.ignorecase else 0


_compiled: "dict[str, re.Pattern[str]]" = {}


def compiled(sp: SecretPattern) -> "re.Pattern[str]":
    pat = _compiled.get(sp.name)
    if pat is None:
        pat = re.compile(sp.regex, _flags(sp))
        _compiled[sp.name] = pat
    return pat


def scan_patterns() -> Iterator[SecretPattern]:
    """证据包扫描口径：不含 redact_only 宽模式。"""
    return (sp for sp in SECRET_PATTERNS if not sp.redact_only)


def _replace_match(sp: SecretPattern, m: "re.Match[str]") -> str:
    whole = m.group(0)
    if not sp.prefix_groups:
        return REDACTED
    # 前缀保留到最后一个前缀组结束处；其后（密钥本体，kv 形态含右引号）全替换。
    cut = max(m.end(g) for g in sp.prefix_groups) - m.start()
    return whole[:cut] + REDACTED


def redact_text(text: object) -> str:
    """对任意文本链式跑全部模式（严格 + 宽模式），历史口径保持不弱化。"""
    s = str(text)
    for sp in SECRET_PATTERNS:
        s = compiled(sp).sub(lambda m, sp=sp: _replace_match(sp, m), s)
    return s


def scan_text(text: object) -> "list[tuple[str, str]]":
    """严格模式扫描：返回 [(pattern_name, matched_substring), ...]。"""
    s = str(text)
    hits: list[tuple[str, str]] = []
    for sp in scan_patterns():
        for m in compiled(sp).finditer(s):
            hits.append((sp.name, m.group(0)))
    return hits


def contains_secret(text: object) -> bool:
    s = str(text)
    return any(compiled(sp).search(s) for sp in scan_patterns())
