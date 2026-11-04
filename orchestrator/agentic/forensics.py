"""取证、观察规范化与版本化缓存指纹（AC-T4-8/9）。

trace.jsonl 每步一行，字段集合钉死（NF-6，禁止增删改名）：
seq,round,step,tool,args_hash,args,obs_sha256,obs_truncated,obs_head,
obs_tail,obs_full_relpath,raw_response_ref,usage,latency_ms,policy_decision,ts_z

seq 语义（M5，2026-10-30 评审）：模型轮 int|null——该轮真过网关实呼记
该轮真实 ledger 整数 seq（含 ok=false 的失败响应，seq 已被账套占用）；
callcache 命中轮记 null。工具行继承最近一次实呼 seq（从未实呼则 null）。

两类内部控制行与工具行同构（字段集不变，用 tool 名前缀 ``__`` 区分，
不新增字段）：``__model_round__``（每次模型调用，挂 raw_response_ref/usage）、
``__condense__``（Condenser 丢弃旧轮内容时的迷路计数事件）。

全量观察只落 trace 存储 ``full/<sha256>``（0600），JSONL 只带头尾切片；
图像 bytes 永不进 JSONL（文本链路本就无图像，二进制 stdout 以替换解码
入切片、原文落 .bin）。resume 只允许从全量存储重建历史观察，禁止为重建
重跑任何工具（工具副作用不可离线重演，AC-T4-9）。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_bytes, ensure_private_dir

# AC-T4-12：20k/50k 截断经验常量的唯一配置点（模型可见观察 + JSONL 切片同值）。
OBS_HEAD_CHARS = 20_000
OBS_TAIL_CHARS = 50_000
_TRUNC_MARKER = (
    "\n... [forensics] 观察已截断：中间 {n} 字符仅存全量存储 "
    "(obs_full_relpath)，对模型不可见 ...\n")

# 缓存逻辑前缀（计划 AC-T4-9 字面量）。CallReplay 磁盘键字符集
# ^[A-Za-z0-9_.-]{1,160}$ 不含冒号，故磁盘键形如 agentic-v3-<sha256>，
# 逻辑名 agentic-v3: 只出现在本模块常量与证据说明中——
# 锁内适配（Phase 2.5）：冒号不落盘，旧 coding_ 键空间一字不动。
#
# 锁内适配（Phase 2.6 Task3，计划 §T3.4-H vs I3-2/AC-T3-5 冲突）：
# 计划 §T3.4-H 要求把 CACHE_VERSION 无条件改成 agentic-v3，而 I3-2 与
# AC-T3-5 要求「四个开关全关时缓存指纹与 Phase 2.5 逐字节相同」——
# 指纹载荷里含 "v" 与逻辑前缀，无条件换纪元两者不可能同时成立。
# 故采用**双纪元**：CACHE_VERSION 常量本身是 v3（AC-T3-6 断言不变），
# 实际纪元由 :func:`cache_epoch` 按 AgenticConfig.any_enabled 选择——全关
# 走 LEGACY(v2) 保持逐字节回滚面，任一开关开启走 v3（I3-3，不得半开
# 共享 v2 键）。Task 4 的判决配置四开关全开，落 v3。
CACHE_VERSION = "agentic-v3"
CACHE_LOGICAL_PREFIX = "agentic-v3:"
CACHE_DISK_PREFIX = "agentic-v3-"
CACHE_VERSION_LEGACY = "agentic-v2"
CACHE_LOGICAL_PREFIX_LEGACY = "agentic-v2:"
CACHE_DISK_PREFIX_LEGACY = "agentic-v2-"
# Phase 2.7 Task 3（D3-7）：ENV_SPEC / 预览探针开启后，prompt 与工具面都变了。
# 若仍落 v3 键空间，会命中「不含 ENV_SPEC 的旧响应」——看起来跑了新路径、
# 实际是旧缓存（证据腐化）。故单开第三纪元；v4 键空间在 .call_cache 下
# 天然为空（验收要求之一）。
CACHE_VERSION_V4 = "agentic-v4"
CACHE_LOGICAL_PREFIX_V4 = "agentic-v4:"
CACHE_DISK_PREFIX_V4 = "agentic-v4-"


def cache_epoch(any_enabled: bool) -> tuple[str, str, str]:
    """按开关面选纪元：任一旧开关开启 → v3；全关 → v2（旧行为逐字节回滚面）。"""
    if type(any_enabled) is not bool:
        raise ValueError("any_enabled 必须是 bool")
    if any_enabled:
        return CACHE_VERSION, CACHE_LOGICAL_PREFIX, CACHE_DISK_PREFIX
    return (CACHE_VERSION_LEGACY, CACHE_LOGICAL_PREFIX_LEGACY,
            CACHE_DISK_PREFIX_LEGACY)


def cache_epoch_for(cfg: Any) -> tuple[str, str, str]:
    """AgenticConfig → 纪元（Phase 2.7 D3-7 的唯一判定入口）。

    新开关（``env_spec`` / ``render_preview``）任一开启即 v4；否则落回
    :func:`cache_epoch` 的旧映射（旧四项任一开启 v3，全关 v2）。
    ``cfg`` 用 ``getattr`` 读取，便于既有的「鸭子类型 cfg」测试桩继续可用。
    """
    if getattr(cfg, "env_spec", False) or getattr(cfg, "render_preview", False):
        return CACHE_VERSION_V4, CACHE_LOGICAL_PREFIX_V4, CACHE_DISK_PREFIX_V4
    return cache_epoch(bool(getattr(cfg, "any_enabled", False)))


def cache_prefixes(version: str) -> tuple[str, str]:
    """纪元版本串 → (逻辑前缀, 磁盘前缀)；未知版本 fail-closed。"""
    if version == CACHE_VERSION:
        return CACHE_LOGICAL_PREFIX, CACHE_DISK_PREFIX
    if version == CACHE_VERSION_LEGACY:
        return CACHE_LOGICAL_PREFIX_LEGACY, CACHE_DISK_PREFIX_LEGACY
    if version == CACHE_VERSION_V4:
        return CACHE_LOGICAL_PREFIX_V4, CACHE_DISK_PREFIX_V4
    raise ValueError(f"未知缓存纪元：{version!r}")

MODEL_ROUND_TOOL = "__model_round__"
CONDENSE_TOOL = "__condense__"
ANSWER_TOOL = "__answer__"
# Phase 2.7 Task 3：ENV_SPEC 装载事件（缺件降级 env_spec_missing 也走这一行）。
# 与其余 ``__`` 控制行同构：字段集不变、seq=null、step=0、不占工具步。
ENV_SPEC_TOOL = "__env_spec__"

# ---------------------------------------------------------------------------
# 观察规范化（每条规则必须配测试；顺序敏感，先剥路径后剥时间/随机量）
# ---------------------------------------------------------------------------

# R1：绝对 workdir 前缀 → <WD>（含 resolve 后的真实路径；先长后短只此一条）。
# R2：ISO-8601 / 常见空格分隔时间戳 → <TS>；纯日期 → <DATE>。
_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# R3：pytest 汇总尾 "1 passed in 0.12s" / time(1) "real 0m0.003s" → <DUR>。
_PYTEST_DUR_RE = re.compile(r" in \d+(?:\.\d+)?s(?=[\s=])")
_TIME_DUR_RE = re.compile(r"\b(real|user|sys)\s+\d+m\d+(?:\.\d+)?s")
# R4：UUID（含 urn 裸形）→ <UUID>。
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}")
# R5：临时路径：mkstemp 形 /tmp/tmpXXXXXX、pytest-of 工作目录 → <TMP>。
# 边界含空白/引号/闭括号：观察文本里临时路径后常接空格或 shell 引号
# （json.dumps 前的纯文本观察，不能只认斜杠/串尾）。
_TMP_MKSTEMP_RE = re.compile(
    r"/tmp/tmp[A-Za-z0-9_.-]{4,}(?=/|$|[\s'\"\)\]])")
_TMP_PYTEST_RE = re.compile(r"/tmp/pytest-of-[^\s'\"\)]+")
# R6：临时端口：127.0.0.1:NNNNN / localhost:NNNNN / "port: 12345" → <PORT>。
_HOSTPORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost):\d{2,5}\b")
# MINOR 1（2026-10-30 评审）：旧正则 [ =:]\d+ 不匹配 "(port: 41001)"
# （冒号后还有空格），而 "port:41001" 命中后替换成 "port==41001" 的非对称
# 美化；规则消费 port 关键字本身，五种分隔写法（含大小写）统一替换为
# port=<PORT> 唯一规范形（关键字一并小写归一）。
_PORT_RE = re.compile(r"\bport[\s:=]+\d{2,5}\b", re.IGNORECASE)
# R7：每次变化的 pid 行（"pid 12345" / "PID=12345"）→ <PID>。
_PID_RE = re.compile(r"(?<=\bpid)[ =:]\d+\b", re.IGNORECASE)
# R8：对象地址十六进制（repr 里的 0x7f...）→ <ADDR>。
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{6,}")
# R9（MINOR 1）：裸 32 位小写 hex（uuid.hex / docker run label uuid.hex 值，
# 如 simpraxis-agentic-run=<32hex>）→ <HEX32>；\b 边界使 40/64 位 sha 不被误吃。
_HEX32_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")
# R10（MINOR 1）：RFC2822 邮件日期头与口语日期（"Oct 29, 2026" 等）→
# <RFC2822>/<DATE>（ISO 形已由 R2 覆盖，规则只追加不改动）。
_MONTHS = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
           r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
           r"Dec(?:ember)?")
_RFC2822_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s+"
    r"\d{2}:\d{2}:\d{2}(?:\s+[+-]\d{4})?")
_SPOKEN_DATE_RE = re.compile(
    rf"(?:\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}|"
    rf"(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}})")
# R11（MINOR 1）：非 localhost 的 host:port（任意 IPv4 / 带点主机名:端口）。
_REMOTE_HOSTPORT_RE = re.compile(
    r"(?:(?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
    r"\.[A-Za-z]{2,}):\d{2,5}\b")
# R12（MINOR 1）：自定义 /tmp 前缀（tempfile.TemporaryDirectory(prefix=...)
# 等非 tmpXXXX 形）；固定名（如探针 /tmp/simpraxis_probe_*.json）被一致
# 归一不影响确定性，跨运行变化的自定义目录不再漏网。
_TMP_CUSTOM_RE = re.compile(
    r"/tmp/[A-Za-z0-9_.@=-]{6,}(?=/|$|[\s'\"\)\],])")


def normalize_observation(text: str, *, workdir: "str | Path | None" = None) -> str:
    """把观察中每次运行都变化的量归一为稳定 token（入指纹前调用）。

    规则编号即上面 R1..R8 常量；新增规则必须同时加测试，且只能追加在
    末尾（顺序变化会改变历史指纹）。
    """
    if not isinstance(text, str):
        raise ValueError("normalize_observation 只接受字符串")
    out = text
    if workdir is not None:
        wd = str(Path(workdir).resolve(strict=False))
        if out.startswith(wd):
            out = "<WD>" + out[len(wd):]
        out = out.replace(wd, "<WD>")
    out = _TS_RE.sub("<TS>", out)
    out = _DATE_RE.sub("<DATE>", out)
    out = _PYTEST_DUR_RE.sub(" in <DUR>s", out)
    out = _TIME_DUR_RE.sub(lambda m: f"{m.group(1)} <DUR>", out)
    out = _UUID_RE.sub("<UUID>", out)
    out = _TMP_PYTEST_RE.sub("<TMP>", out)
    out = _TMP_MKSTEMP_RE.sub("<TMP>", out)
    out = _HOSTPORT_RE.sub("<HOST>:<PORT>", out)
    out = _PORT_RE.sub("port=<PORT>", out)
    out = _PID_RE.sub("=<PID>", out)
    out = _ADDR_RE.sub("<ADDR>", out)
    out = _HEX32_RE.sub("<HEX32>", out)                       # R9
    out = _RFC2822_RE.sub("<RFC2822>", out)                  # R10
    out = _SPOKEN_DATE_RE.sub("<DATE>", out)                 # R10
    out = _REMOTE_HOSTPORT_RE.sub("<HOST>:<PORT>", out)      # R11
    out = _TMP_CUSTOM_RE.sub("<TMP>", out)                   # R12
    return out


def canonical_json(payload: Any) -> str:
    """指纹/args_hash 唯一序列化（sort_keys、ensure_ascii=False、default=str）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=str, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 版本化缓存指纹（agentic-v2；旧 coding_ 键空间不共享、不改一字）
# ---------------------------------------------------------------------------

GENESIS_PREFIX = "0" * 64


def advance_prefix(prefix_hex: str, *, step: int, tool: str,
                   args_canonical: str, norm_obs_sha256: str) -> str:
    """工具轨迹前缀链：前一步链值 + 本步（步序/工具/规范化参数/规范化观察哈希）。"""
    h = hashlib.sha256()
    h.update(prefix_hex.encode("utf-8"))
    h.update(b"|")
    h.update(canonical_json({
        "step": step, "tool": tool,
        "args": args_canonical, "obs": norm_obs_sha256,
    }).encode("utf-8"))
    return h.hexdigest()


def agentic_cache_fingerprint(*, stage: str, model: str, system_prompt: str,
                              task: str, round_idx: int, prefix_hex: str,
                              max_tokens: int, temperature: float,
                              thinking: dict | None,
                              version: str = CACHE_VERSION,
                              scratchpad_sha256: str = "",
                              scratchpad_chars: int = 0,
                              prompt_extra_sha256: str = "") -> str:
    """单轮模型调用的逻辑指纹（含工具轨迹前缀 + 规范化历史）。

    Phase 2.6 Task3（D3-7）：scratchpad 内容必须进指纹，否则不同
    scratchpad 撞同一缓存键会返回上一轮答案（静默腐化）。空串时（v3
    内）键仍参与计算，保证「有/无 scratchpad」的键必然可分。v2 载荷
    逐字节保持旧形状（三个新键不出现），这是 I3-2 的可靠回滚面。
    """
    payload: dict = {
        "v": version,
        "stage": stage, "model": model,
        "system_prompt": system_prompt, "task": task,
        "round": round_idx, "prefix": prefix_hex,
        "max_tokens": max_tokens, "temperature": temperature,
        "thinking": thinking or {},
    }
    if version != CACHE_VERSION_LEGACY:
        payload["scratchpad_sha256"] = scratchpad_sha256
        payload["scratchpad_chars"] = scratchpad_chars
        # 追授尾指令等提示面附加段也必须进指纹（提示变 → 键必变）。
        payload["prompt_extra_sha256"] = prompt_extra_sha256
    return sha256_text(canonical_json(payload))


def to_disk_key(fingerprint_hex: str, *,
                prefix: str = CACHE_DISK_PREFIX) -> str:
    """逻辑指纹 → CallReplay 合法磁盘键（agentic-v3-<sha256>）。"""
    key = prefix + fingerprint_hex
    if len(key) > 160:  # CallReplay 键正则上限；当前 12+64=76，防御性守卫。
        raise ValueError(f"agentic cache key 超长：{len(key)} > 160")
    return key


def truncate_observation(text: str) -> tuple[str, str, str, bool]:
    """返回 (模型可见文本, head, tail, truncated)；全量原文另行落盘。"""
    if len(text) <= OBS_HEAD_CHARS + OBS_TAIL_CHARS + 64:
        return text, text, "", False
    head = text[:OBS_HEAD_CHARS]
    tail = text[-OBS_TAIL_CHARS:]
    middle = len(text) - OBS_HEAD_CHARS - OBS_TAIL_CHARS
    visible = head + _TRUNC_MARKER.format(n=middle) + tail
    return visible, head, tail, True


def decode_observation(data: bytes) -> tuple[str, bool]:
    """stdout 等字节观察转文本：utf-8 替换解码；(text, was_binary)。

    NUL 是合法 UTF-8（U+0000），replace 解码不会消除它；JSONL 禁止 NUL，
    故文本渲染再统一替换为 U+FFFD。全量 bytes 仍原样落 full/（sha/重放
    对账按 bytes 不受影响），只让模型可见文本与 JSON 切片无 NUL。
    """
    is_binary = b"\x00" in data
    text = data.decode("utf-8", errors="replace").replace("\x00", "�")
    return text, is_binary


# ---------------------------------------------------------------------------
# trace 存储
# ---------------------------------------------------------------------------

@dataclass
class StoredObservation:
    sha256: str
    relpath: str
    head: str
    tail: str
    truncated: bool
    is_binary: bool


class TraceStore:
    """agentic_traces/<thread>/ 下的 trace.jsonl + full/ + raw/ 存储。"""

    def __init__(self, root: "str | Path", thread_id: str):
        self.root = ensure_private_dir(Path(root) / thread_id)
        self.full_dir = ensure_private_dir(self.root / "full")
        self.raw_dir = ensure_private_dir(self.root / "raw")
        self.trace_path = self.root / "trace.jsonl"
        if not self.trace_path.exists():
            atomic_write_bytes(self.trace_path, b"", mode=0o600)

    # ---- 全量观察 / 原始响应落盘 --------------------------------------

    def store_full(self, data: "str | bytes") -> StoredObservation:
        if isinstance(data, str):
            raw = data.encode("utf-8")
            text = data
            is_binary = False
        else:
            raw = bytes(data)
            text, is_binary = decode_observation(raw)
        digest = sha256_bytes(raw)
        ext = "bin" if is_binary else "txt"
        relpath = f"full/{digest}.{ext}"
        target = self.root / relpath
        if not target.exists():  # 同哈希幂等，不重复写
            atomic_write_bytes(target, raw, mode=0o600)
        visible, head, tail, truncated = truncate_observation(text)
        return StoredObservation(digest, relpath, head, tail, truncated,
                                 is_binary)

    def store_raw_response(self, raw_text: str) -> str:
        """模型原始响应落 raw/，返回相对 trace 根的引用（raw_response_ref）。"""
        digest = sha256_text(raw_text)
        relpath = f"raw/{digest}.txt"
        target = self.root / relpath
        if not target.exists():
            atomic_write_bytes(target, raw_text.encode("utf-8"), mode=0o600)
        return relpath

    def read_full(self, relpath: str) -> bytes:
        """resume 重建历史观察的唯一读路径（禁止重跑工具）。

        MINOR 5（2026-10-30 评审）：full/ 内任何符号链接（含指向同目录
        其他全量件的链接）一律 lstat 拒绝——只允许本存储原子写出的常规
        文件，防 planted symlink 把重建读重定向到任意宿主文件。
        """
        p = self.root / relpath
        if p.is_symlink():
            raise ValueError(f"全量观察引用是符号链接，拒绝读取：{relpath}")
        resolved = p.resolve(strict=False)
        if resolved.parent != self.full_dir:
            raise ValueError(f"全量观察引用越界：{relpath}")
        return resolved.read_bytes()

    # ---- JSONL 原子追加（0600 + flock + fsync） ------------------------

    def append_line(self, line: dict[str, Any]) -> None:
        expected = [
            "seq", "round", "step", "tool", "args_hash", "args",
            "obs_sha256", "obs_truncated", "obs_head", "obs_tail",
            "obs_full_relpath", "raw_response_ref", "usage",
            "latency_ms", "policy_decision", "ts_z",
        ]
        if list(line.keys()) != expected:
            raise ValueError(
                f"trace 行字段集必须逐字为 {expected}，收到 {list(line.keys())}")
        # M5（2026-10-30 评审）：seq 语义放宽为 int|null——实呼轮记该轮真实
        # ledger 整数 seq；缓存命中轮记 null；bool 是 int 子类，显式拒。
        seqv = line["seq"]
        if seqv is not None and (not isinstance(seqv, int) or isinstance(seqv, bool)):
            raise ValueError(
                f"trace seq 必须是 int 或 null（实呼 ledger seq / 缓存命中 null），"
                f"收到 {type(seqv).__name__}")
        # 图像/二进制 bytes 不可能进 JSON：args 经 schema 只含 str/int/list[str]，
        # obs 切片是替换解码文本。json.dumps 总会把 U+0000 转义成 \u0000，
        # 故在序列化前对字符串叶子递归扫一遍（fail-closed 防回归）。
        def _scan_nul(obj: Any) -> None:
            if isinstance(obj, str):
                if "\x00" in obj:
                    raise ValueError("trace 行含 NUL（疑似二进制泄漏，禁止入 JSONL）")
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    _scan_nul(k); _scan_nul(v)
            elif isinstance(obj, (list, tuple)):
                for v in obj:
                    _scan_nul(v)

        _scan_nul(line)
        payload = (json.dumps(line, ensure_ascii=False, default=str) + "\n")
        if "\x00" in payload:  # 双保险：序列化后再断言一次。
            raise ValueError("trace 行含 NUL（疑似二进制泄漏，禁止入 JSONL）")
        fd = os.open(str(self.trace_path),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def load_lines(self) -> list[dict[str, Any]]:
        if not self.trace_path.exists():
            return []
        lines = []
        for raw in self.trace_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
        return lines

    # ---- 高层记录 -------------------------------------------------------

    def record(self, *, seq: "int | None", round_idx: int, step: int, tool: str,
               args: dict[str, Any], observation: "str | bytes",
               policy: dict[str, str], latency_ms: int,
               raw_response_ref: "str | None" = None,
               usage: "list[int] | None" = None) -> dict[str, Any]:
        stored = self.store_full(observation)
        line = {
            "seq": seq,
            "round": round_idx,
            "step": step,
            "tool": tool,
            "args_hash": sha256_text(canonical_json(args)),
            "args": args,
            "obs_sha256": stored.sha256,
            "obs_truncated": stored.truncated,
            "obs_head": stored.head,
            "obs_tail": stored.tail,
            "obs_full_relpath": stored.relpath,
            "raw_response_ref": raw_response_ref,
            "usage": usage,
            "latency_ms": latency_ms,
            "policy_decision": policy,
            "ts_z": utc_now_iso_z(),
        }
        self.append_line(line)
        return line

    def rebuild_observations(self) -> dict[int, tuple[str, dict[str, Any]]]:
        """resume：从全量存储重建 {step: (full_text, line)}；绝不重跑工具。

        内部控制行（tool 以 ``__`` 开头，step=0）不进映射；全量文件缺失/
        不可读按 ValueError fail-closed（调用方据此诚实重出生，不得伪装命中）。
        """
        out: dict[int, tuple[str, dict[str, Any]]] = {}
        for line in self.load_lines():
            if str(line.get("tool", "")).startswith("__"):
                continue
            rel = line.get("obs_full_relpath")
            if not rel or not rel.startswith("full/"):
                raise ValueError(
                    f"续写失败：step {line.get('step')} 缺少全量观察引用")
            try:
                raw = self.read_full(rel)
            except OSError as exc:
                raise ValueError(
                    f"续写失败：step {line.get('step')} 全量观察不可读"
                    f"（{rel}）：{exc}；不得重跑工具补齐，按重出生处理") from exc
            text, _ = decode_observation(raw)
            out[int(line["step"])] = (text, line)
        return out

    def tool_decision_sequence(self) -> list[dict[str, Any]]:
        """工具决策序列（去内部控制行）：重放一致性断言用。"""
        return [
            {"step": ln["step"], "tool": ln["tool"],
             "args_hash": ln["args_hash"],
             "denied": ln["policy_decision"]["decision"] == "deny"}
            for ln in self.load_lines()
            if not ln["tool"].startswith("__")
        ]


def iter_trajectory(lines: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """按 step 序过滤出真实工具行（指纹前缀链重放用）。"""
    for ln in lines:
        if isinstance(ln, dict) and not str(ln.get("tool", "")).startswith("__"):
            yield ln
