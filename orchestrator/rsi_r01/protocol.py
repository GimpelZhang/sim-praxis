"""T6 协议 12 请求校准（AC28）：固定顺序、真实工具、单链失败即停、不补考。

计划 §6.1 的硬性组成（与 LOCK `facts.order.protocol_chain` 逐字一致）：

======  ==============  =====  ===================================
slot    kind            posts  成功条件
======  ==============  =====  ===================================
1/2     short_text      1      非空可解析正文 + nonce 回显 + usage/stop_reason
3/4     tool_roundtrip  2      第1轮产出合法动作 → 容器真实读文件 → 第2轮回报标记
5/6     long_context    3      第2/3轮输入分别落在 15000–18000 / 18000–20000 token
======  ==============  =====  ===================================

计数纪律（§6.2）：

* 计数对象是**完整序列化请求**（``SendPlan.serialized``），计数后字节 hash 必须与发网件一致；
* 每 POST 输入 ≤ 20000：字节上界 ``tokens ≤ utf8_bytes`` 恒成立，作为**硬门**；
* 长链区间需要**下界证明**：字节下界与「bytes ≤ 18000」不可同时成立，故用「本地分词器
  + 实测供应商开销带」。供应商无 count_tokens（2026-11-09 实测 404），前 6 个 POST 的
  ``usage.input_tokens`` 与分词器计数逐条比对；全部 delta ≥ 0 且 max−min ≤
  ``OVERHEAD_SPREAD_MAX`` 才把 ``[tok+min_delta, tok+max_delta]`` 当区间；任一不满足 ⇒
  长链 **零 POST** 并记 ``BLOCKED_CONTEXT_MEASUREMENT``（不把短输入冒充长链）。
* 单逻辑请求至多 1 POST；``UNKNOWN_SENT`` 不重发；发网前拒绝记 preflight_blocked（不占 seq）。

先登记后跑：``--pre-register`` 在**任何 POST 之前**把 12 个请求的**完整发网件**写进
``protocol_prereg.json``（含逐件 sha256 与计数），``--run`` 逐件校验 hash 后只发预登记字节。
工具动作面只有 ``read_file``；标记内容由 nonce 确定性派生，故工具结果文本在预登记时即已
确定——真实容器读出的 sha256 必须与预登记一致，否则链失败。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.rsi_r01 import attempts as AT
from orchestrator.rsi_r01 import campaign as C
from orchestrator.rsi_r01 import lock as L
from orchestrator.rsi_r01 import paid as P
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import transport as T
from orchestrator.rsi_r01.observations import IMAGE_REF

REPORT_SCHEMA = "simpraxis.rsi_r01.protocol_report.v1"
PREREG_SCHEMA = "simpraxis.rsi_r01.protocol_prereg.v1"

#: 与 LOCK ``facts.order.protocol_chain`` 对齐的固定链（顺序即执行顺序）。
CHAIN: "tuple[tuple[int, str, str, int], ...]" = (
    (1, "short_text", "S1", 1),
    (2, "short_text", "S2", 1),
    (3, "tool_roundtrip", "T1", 2),
    (4, "tool_roundtrip", "T2", 2),
    (5, "long_context_tool_chain", "L1", 3),
    (6, "long_context_tool_chain", "L2", 3),
)
PROTOCOL_POSTS = sum(p for _, _, _, p in CHAIN)


def budget_slot_id(slot_id: str, slot: int) -> str:
    """槽账唯一键（reviewer-r3 CRITICAL）。

    ``SlotBudget`` 是「键即事实」的账：``claim`` 建立的键必须与
    ``record_post``/``mark_preflight_blocked`` 使用**同一个字面串**，否则第一条真实
    POST 记账即抛 ``SlotExhausted``。这里只允许经本函数生成键，禁止各处手拼。
    """
    return f"{slot_id}:{slot}"
assert PROTOCOL_POSTS == 12

#: 长链第 2/3 轮输入的目标区间（token，闭区间）。
LONG_TARGETS = {5: ((15000, 18000), (18000, 20000)),
                6: ((15000, 18000), (18000, 20000))}

#: 供应商开销带的允许离散度（token）：6 样本 max−min 超过即拒绝采用该区间。
OVERHEAD_SPREAD_MAX = 16
# 长链填充条数上限：任何自洽分词器要到 20k token 都远低于此；超过即视为计数失配。
MAX_FILLER_RECORDS = 40000

SHORT_MAX_OUT = 512
TOOL_MAX_OUT = 1024
LONG_MAX_OUT = 2048

MARKER_NAME = "marker.txt"
TOOL_SCRIPT = '''\
import hashlib, json, os, sys
name = sys.argv[1] if len(sys.argv) > 1 else "marker.txt"
path = os.path.join("/work", os.path.basename(name))
with open(path, "rb") as fh:
    raw = fh.read()
doc = {"ok": True, "name": os.path.basename(name), "bytes": len(raw),
       "sha256": hashlib.sha256(raw).hexdigest(),
       "content": raw.decode("utf-8").strip()}
print("RESULT_JSON " + json.dumps(doc, sort_keys=True))
'''


# ---------------------------------------------------------------------------
# 计数
# ---------------------------------------------------------------------------

class TokenizerUnavailable(RuntimeError):
    """分词器不可用（缺件/哈希不符）：拒绝用未验证的计数器放行长链。"""


@dataclass(frozen=True)
class Tokenizer:
    """本地分词器（钉 sha256）：只做计数，不触网。"""

    path: Path
    sha256: str
    _tok: Any = None

    @classmethod
    def load(cls, path: "str | Path", expected_sha256: str | None = None) -> "Tokenizer":
        from tokenizers import Tokenizer as _Tk
        p = Path(path)
        if not p.is_file():
            raise TokenizerUnavailable(f"分词器缺件：{p}")
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise TokenizerUnavailable(
                f"分词器哈希不符：{p} sha256={digest} != {expected_sha256}")
        return cls(path=p, sha256=digest, _tok=_Tk.from_file(str(p)))

    def count(self, text: str) -> int:
        return len(self._tok.encode(text, add_special_tokens=False).ids)


def render_text(plan: T.SendPlan) -> str:
    """把发网件渲染成被计数的文本：system + 每条消息正文 + tools schema。"""
    req = plan.request
    parts: "list[str]" = []
    sysm = req.get("system")
    if isinstance(sysm, str):
        parts.append(sysm)
    elif isinstance(sysm, list):
        for blk in sysm:
            if isinstance(blk, Mapping) and blk.get("type") == "text":
                parts.append(str(blk.get("text") or ""))
    for msg in req.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if not isinstance(blk, Mapping):
                    continue
                if blk.get("type") == "text":
                    parts.append(str(blk.get("text") or ""))
                elif blk.get("type") == "tool_result":
                    inner = blk.get("content")
                    if isinstance(inner, str):
                        parts.append(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, Mapping) and sub.get("type") == "text":
                                parts.append(str(sub.get("text") or ""))
    tools = req.get("tools")
    if tools:
        parts.append(json.dumps(tools, sort_keys=True, ensure_ascii=False))
    return "\n".join(parts)


def count_plan(plan: T.SendPlan, *, tokenizer: Tokenizer | None = None,
               overhead: "tuple[int, int] | None" = None,
               interval: "tuple[int, int] | None" = None,
               cap: int = S.MAX_INPUT_TOKENS) -> T.Accounting:
    """完整请求计数：字节上界恒为硬门；有分词器+开销带时给出可证区间。"""
    n = plan.n_bytes
    upper = n
    lower: "int | None" = None
    reasons: "list[str]" = []
    if tokenizer is not None and overhead is not None:
        tok = tokenizer.count(render_text(plan))
        lo_oh, hi_oh = int(overhead[0]), int(overhead[1])
        if hi_oh < lo_oh:
            raise ValueError(f"开销带非法：[{lo_oh},{hi_oh}]")
        lower = max(0, tok + lo_oh)
        upper = min(n, tok + hi_oh)
        if lower > upper:
            return T.Accounting(T.AccountingVerdict.BLOCKED_TOKEN_ACCOUNTING,
                                n, upper, lower, interval, False, plan.sha256,
                                (S.ReasonCode.TOKEN_OVER_CAP,))
    if upper > cap:
        return T.Accounting(T.AccountingVerdict.TOKEN_OVER_CAP, n, upper,
                            lower, interval, False, plan.sha256,
                            (S.ReasonCode.TOKEN_OVER_CAP,))
    interval_ok = True
    verdict = T.AccountingVerdict.OK
    if interval is not None:
        lo, hi = int(interval[0]), int(interval[1])
        if lower is None:
            interval_ok = False
            verdict = T.AccountingVerdict.BLOCKED_CONTEXT_MEASUREMENT
            reasons.append("LOWER_BOUND_UNPROVABLE")
        elif lower < lo or upper > hi:
            interval_ok = False
            verdict = T.AccountingVerdict.BLOCKED_CONTEXT_MEASUREMENT
            reasons.append("INTERVAL_NOT_PROVEN")
    return T.Accounting(verdict, n, upper, lower, interval, interval_ok,
                        plan.sha256, tuple(reasons))


# ---------------------------------------------------------------------------
# 请求内容（确定性；预登记对象）
# ---------------------------------------------------------------------------

SYSTEM_SHORT = ("You are a calibration probe. Reply with a single JSON object "
                "on one line and nothing else.")

VOCAB = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
         "nu xi omicron pi rho sigma tau upsilon phi chi psi omega").split()


def marker_for(nonce: str) -> str:
    """由 nonce 确定性派生标记内容（32 hex）：工具结果文本因此在预登记时即确定。"""
    return hashlib.sha256(("marker:" + nonce).encode("utf-8")).hexdigest()[:32]


def tool_result_for(content: str) -> "dict[str, str]":
    raw = (content + "\n").encode("utf-8")
    return {"content": content, "sha256": hashlib.sha256(raw).hexdigest()}


def _synthetic_records(seed: int, n: int) -> str:
    """确定性合成公开记录（无隐藏答案、无真实数据）：把长链撑到目标 token。"""
    out: "list[str]" = []
    v = seed
    for i in range(n):
        v = (1103515245 * v + 12345) % (2 ** 31)
        w1 = VOCAB[v % len(VOCAB)]
        w2 = VOCAB[(v >> 7) % len(VOCAB)]
        w3 = VOCAB[(v >> 13) % len(VOCAB)]
        out.append(f"record {i:05d}: {w1} {w2} {w3} checksum={(v * 2654435761) % 1000003:07d}")
    return "\n".join(out)


def short_prompt(nonce: str) -> str:
    return ('Reply exactly {"nonce": "<nonce>", "ok": true} with the nonce '
            f"{nonce}.")


TOOL_SCHEMA = [{
    "name": "read_file",
    "description": "Read a file from the probe work directory and return its content hash.",
    "input_schema": {"type": "object",
                     "properties": {"name": {"type": "string"}},
                     "required": ["name"]},
}]


def tool_prompt(nonce: str) -> str:
    return ('You have one tool, read_file. Reply with a single JSON object '
            '{"action": {"tool": "read_file", "name": "marker.txt"}, '
            f'"nonce": "{nonce}"}} and nothing else.')


def tool_follow_prompt(nonce: str, doc: Mapping[str, str]) -> str:
    return ('The tool read_file returned: {"content": "%s", "sha256": "%s"}. '
            'Reply with {"nonce": "<nonce>", "value": "<content>", '
            '"sha256": "<sha256>"} echoing nonce %s.'
            % (doc["content"], doc["sha256"], nonce))


def long_stage1_prompt(nonce: str) -> str:
    return ('You are running a synthetic long-context probe. Read the work file '
            'with read_file and reply with {"action": {"tool": "read_file", '
            f'"name": "marker.txt"}}, "nonce": "{nonce}"}}.')


def long_stage2_prompt(nonce: str, doc: Mapping[str, str], filler: str) -> str:
    return ("Synthetic tool archive (public, no secrets):\n" + filler +
            '\n\nEarlier tool result: {"content": "%s", "sha256": "%s"}'
            '\n\nReply with {"nonce": "<nonce>", "value": "<content>", '
            '"sha256": "<sha256>"} using nonce %s.'
            % (doc["content"], doc["sha256"], nonce))


def long_stage3_prompt(nonce: str, doc: Mapping[str, str], prior_request: str,
                       filler: str) -> str:
    return ("Synthetic tool archive, part 2 (public, no secrets):\n" + filler +
            "\n\nEarlier probe instruction (verbatim):\n" + prior_request +
            '\n\nLatest tool result: {"content": "%s", "sha256": "%s"}'
            '\n\nReply with {"nonce": "<nonce>", "value": "<content>", '
            '"sha256": "<sha256>", "seen": true} using nonce %s.'
            % (doc["content"], doc["sha256"], nonce))


def _plan(model: str, body: str, *, tools: bool, max_out: int) -> T.SendPlan:
    return T.plan_from_messages(
        system=SYSTEM_SHORT, messages=[{"role": "user", "content": body}],
        tools=TOOL_SCHEMA if tools else None,
        max_output_tokens=max_out, model=model)


def _size_filler(*, model: str, tokenizer: "Tokenizer | None", target_tokens: int,
                 build: Callable[[int], str], max_out: int) -> "tuple[int, str, T.SendPlan]":
    """把合成记录条数调到目标 token 附近（确定性；无分词器时退化到条数估计）。"""
    if tokenizer is None:
        n = max(1, int(target_tokens / 12))
        body = build(n)
        return n, body, _plan(model, body, tools=True, max_out=max_out)
    n = max(1, int(target_tokens / 12))
    body = build(n)
    plan = _plan(model, body, tools=True, max_out=max_out)
    for _ in range(8):
        got = tokenizer.count(render_text(plan))
        if not isinstance(got, int) or got <= 0:
            raise ProtocolBlocked(
                "BLOCKED_TOKEN_ACCOUNTING",
                f"分词器计数非正（{got!r}）：拒绝用它调整长链体积")
        if abs(got - target_tokens) <= 50:
            break
        n = max(1, int(round(n * target_tokens / got)))
        if n > MAX_FILLER_RECORDS:
            raise ProtocolBlocked(
                "BLOCKED_TOKEN_ACCOUNTING",
                f"长链填充需 {n} 条记录 > 帽 {MAX_FILLER_RECORDS}："
                "分词器计数与供应商不匹配，拒绝生成")
        body = build(n)
        plan = _plan(model, body, tools=True, max_out=max_out)
    return n, body, plan


def build_prereg(*, model: str, tokenizer: "Tokenizer | None",
                 nonces: "Mapping[int, str] | None" = None) -> dict[str, Any]:
    """构造 12 个请求的**完整发网件**与计数（预登记），不发网。"""
    nonces = dict(nonces or {})
    posts: "list[dict[str, Any]]" = []
    for slot, kind, chain, count in CHAIN:
        nonce = nonces.get(slot) or os.urandom(8).hex()
        content = marker_for(nonce)
        doc = tool_result_for(content)
        if kind == "short_text":
            plan = _plan(model, short_prompt(nonce), tools=False,
                         max_out=SHORT_MAX_OUT)
            posts.append({"slot": slot, "chain": chain, "kind": kind,
                          "index": 1, "nonce": nonce, "interval": None,
                          "request": plan.request,
                          "max_output_tokens": plan.max_output_tokens})
        elif kind == "tool_roundtrip":
            p1 = _plan(model, tool_prompt(nonce), tools=True,
                       max_out=TOOL_MAX_OUT)
            posts.append({"slot": slot, "chain": chain, "kind": kind,
                          "index": 1, "nonce": nonce, "interval": None,
                          "request": p1.request,
                          "max_output_tokens": p1.max_output_tokens})
            p2 = _plan(model, tool_follow_prompt(nonce, doc), tools=True,
                       max_out=TOOL_MAX_OUT)
            posts.append({"slot": slot, "chain": chain, "kind": kind,
                          "index": 2, "nonce": nonce, "interval": None,
                          "request": p2.request,
                          "max_output_tokens": p2.max_output_tokens})
        else:
            p1 = _plan(model, long_stage1_prompt(nonce), tools=True,
                       max_out=LONG_MAX_OUT)
            posts.append({"slot": slot, "chain": chain, "kind": kind,
                          "index": 1, "nonce": nonce, "interval": None,
                          "request": p1.request,
                          "max_output_tokens": p1.max_output_tokens})
            targets = LONG_TARGETS[slot]
            # 目标中点偏向区间内部：留开销带余量（band 上界由前 6 POST 实测）
            for idx, interval in ((2, targets[0]), (3, targets[1])):
                aim = (interval[0] + interval[1]) // 2
                seed = slot * 100 + idx
                if idx == 2:
                    build = lambda n, seed=seed: long_stage2_prompt(
                        nonce, doc, _synthetic_records(seed, n))
                else:
                    build = lambda n, seed=seed: long_stage3_prompt(
                        nonce, doc, long_stage1_prompt(nonce),
                        _synthetic_records(seed, n))
                n_rec, _body, plan = _size_filler(
                    model=model, tokenizer=tokenizer, target_tokens=aim,
                    build=build, max_out=LONG_MAX_OUT)
                posts.append({"slot": slot, "chain": chain, "kind": kind,
                              "index": idx, "nonce": nonce,
                              "interval": list(interval),
                              "filler_records": n_rec,
                              "request": plan.request,
                              "max_output_tokens": plan.max_output_tokens})
    for rec in posts:
        plan = T.SendPlan(request=rec["request"],
                          max_output_tokens=rec["max_output_tokens"])
        rec["request_sha256"] = plan.sha256
        rec["bytes"] = plan.n_bytes
        if tokenizer is not None:
            rec["tokenizer_sha256"] = tokenizer.sha256
            rec["tokenizer_tokens"] = tokenizer.count(render_text(plan))
    return {"schema": PREREG_SCHEMA, "model": model,
            "created_at_utc": C._utc_now(),
            "tokenizer": ({"path": str(tokenizer.path), "sha256": tokenizer.sha256}
                          if tokenizer else None),
            "overhead_spread_max": OVERHEAD_SPREAD_MAX,
            "posts": posts,
            "post_count": len(posts),
            "note": ("预登记发生在任何 POST 之前：12 个发网件完整入册；长链内容为确定性"
                     "合成公开记录，不含隐藏答案；标记内容由 nonce 派生，故工具结果"
                     "文本在预登记时已确定。区间证明依赖前 6 POST 的 usage 比对。")}


# ---------------------------------------------------------------------------
# 工具真实执行（固定镜像容器，无网络、只读根）
# ---------------------------------------------------------------------------

class ToolError(RuntimeError):
    """工具动作非法或容器执行失败（链失败，不补考）。"""


def tool_argv(*, work_dir: Path, data_dir: Path, out_dir: Path,
              name: str) -> list[str]:
    return [
        "docker", "run", "--rm",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--pids-limit={S.ANALYSIS_PIDS}",
        f"--memory={S.ANALYSIS_MEMORY}",
        f"--cpus={S.ANALYSIS_CPUS}",
        "--read-only",
        "--tmpfs=/tmp:rw,size=64m",
        "--user=1000:1000",
        "-e", "HOME=/tmp",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{work_dir}:/work:ro",
        "-v", f"{data_dir}:/data:ro",
        "-v", f"{out_dir}:/out:rw",
        "-w", "/out",
        IMAGE_REF, "python3", "/work/tool.py", name,
    ]


@dataclass
class ToolExec:
    """一个链专属的工具执行器：work/out 目录隔离，动作面只有 read_file。"""

    root: Path

    def prepare(self, *, name: str = MARKER_NAME,
                content: "str | None" = None) -> "dict[str, str]":
        work = self.root / "work"
        out = self.root / "out"
        data = self.root / "data"
        for d in (work, out, data):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        body = (content if content is not None else os.urandom(16).hex()) + "\n"
        (work / name).write_text(body, encoding="utf-8")
        os.chmod(work / name, 0o600)
        (work / "tool.py").write_text(TOOL_SCRIPT, encoding="utf-8")
        os.chmod(work / "tool.py", 0o600)
        return {"name": name, "content": body.strip(),
                "host_sha256": hashlib.sha256(
                    (work / name).read_bytes()).hexdigest()}

    def read(self, name: str, *, timeout_s: int = 120) -> "dict[str, Any]":
        if "/" in name or "\\" in name or ".." in name:
            raise ToolError(f"工具动作路径非法：{name!r}")
        work = self.root / "work"
        data = self.root / "data"
        out = self.root / "out"
        if shutil.which("docker") is None:
            raise ToolError("docker 不可用：工具必须真实执行在固定镜像容器里")
        started = time.time()
        try:
            proc = subprocess.run(tool_argv(work_dir=work, data_dir=data,
                                            out_dir=out, name=name),
                                  capture_output=True, text=True,
                                  timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"工具容器超时 {timeout_s}s") from exc
        line = ""
        for ln in (proc.stdout or "").splitlines():
            if ln.startswith("RESULT_JSON "):
                line = ln[len("RESULT_JSON "):]
                break
        if not line:
            raise ToolError(f"工具容器无 RESULT_JSON（rc={proc.returncode}）")
        doc = json.loads(line)
        doc.update({"rc": proc.returncode, "wall_s": round(time.time() - started, 3),
                    "image": IMAGE_REF,
                    "executed_in_container": bool(doc.get("ok"))})
        return doc


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

class ProtocolBlocked(RuntimeError):
    """链失败/预登记不符/预算不允：停止后续 slot（记 CANCELLED，不补考）。"""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def parse_json_object(text: str) -> "dict[str, Any] | None":
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        doc = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return doc if isinstance(doc, dict) else None


@dataclass(kw_only=True)
class ProtocolRunner:
    """按 LOCK 固定链执行 12 请求；所有判断在发网前完成。"""

    ctx: C.CampaignContext
    model: str
    tokenizer: "Tokenizer | None" = None
    transport: "P.PaidTransport | None" = None
    budget: Any = None                     # attempts.SlotBudget
    root: "Path | None" = None
    tool_root: "Path | None" = None

    def __post_init__(self) -> None:
        self.root = Path(self.root) if self.root else self.ctx.subdir("analysis") / "t6_protocol"
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.transport = self.transport or P.PaidTransport(self.ctx)
        self.tool_root = (Path(self.tool_root) if self.tool_root
                          else self.root / "tools")
        self.posts: "list[dict[str, Any]]" = []
        self.slot_rows: "list[dict[str, Any]]" = []
        self.cancelled: "list[dict[str, Any]]" = []

    # -- 预登记 -----------------------------------------------------------
    def prereg_relpath(self) -> str:
        return "analysis/t6_protocol/protocol_prereg.json"

    def report_relpath(self) -> str:
        return "analysis/t6_protocol/protocol_report.json"

    def write_prereg(self, *, nonces: "Mapping[int, str] | None" = None) -> "dict[str, Any]":
        doc = build_prereg(model=self.model, tokenizer=self.tokenizer,
                           nonces=nonces)
        C.write_json(self.ctx, self.prereg_relpath(), doc)
        return doc

    def load_prereg(self) -> "dict[str, Any]":
        path = self.ctx.root / self.prereg_relpath()
        if not path.is_file():
            raise ProtocolBlocked("PREREG_MISSING",
                                  f"缺少预登记（先 --pre-register）：{path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != PREREG_SCHEMA:
            raise ProtocolBlocked("PREREG_MISMATCH", "schema 不符")
        if doc.get("model") != self.model:
            raise ProtocolBlocked("PREREG_MISMATCH",
                                  f"模型不符：{doc.get('model')} != {self.model}")
        tok = doc.get("tokenizer") or {}
        # reviewer-r3 L3：登记与否必须双向一致——预登记无 tokenizer 而 runner 有
        # （或反之）不得当作「无需比对」，否则长链可能用未登记的计数器跑。
        if bool(tok) != (self.tokenizer is not None):
            raise ProtocolBlocked(
                "PREREG_MISMATCH",
                f"分词器登记不符：prereg={'有' if tok else '无'}"
                f"/runner={'有' if self.tokenizer is not None else '无'}")
        if tok and tok.get("sha256") != self.tokenizer.sha256:
            raise ProtocolBlocked("PREREG_MISMATCH", "分词器哈希不符")
        posts = doc.get("posts") or []
        if len(posts) != PROTOCOL_POSTS:
            raise ProtocolBlocked("PREREG_MISMATCH",
                                  f"预登记请求数 {len(posts)} != {PROTOCOL_POSTS}")
        for rec in posts:
            if not isinstance(rec, Mapping):
                raise ProtocolBlocked("PREREG_MISMATCH", "posts 条目不是映射")
            # reviewer-r3 LOW：非数值字段不得以 ValueError 逃逸——一律归为
            # 预登记不符（fail-closed 且可分类）。
            try:
                mot = int(rec["max_output_tokens"])
                req = rec["request"]
                req_mot = int(req["max_tokens"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProtocolBlocked(
                    "PREREG_MISMATCH",
                    f"预登记数值字段不可解析（{type(exc).__name__}: {exc}）") from exc
            # reviewer-r3 LOW：max_output_tokens 字段不在 request hash 覆盖范围内，
            # 必须与 request.max_tokens 显式相等，否则可单独放大输出帽。
            if mot != req_mot:
                raise ProtocolBlocked(
                    "PREREG_MISMATCH",
                    f"max_output_tokens={mot} != request.max_tokens={req_mot}")
            plan = T.SendPlan(request=req, max_output_tokens=mot)
            if plan.sha256 != rec.get("request_sha256"):
                raise ProtocolBlocked(
                    "PREREG_MISMATCH",
                    f"slot {rec['slot']}#{rec['index']} 请求 hash 不符")
            # reviewer-r3 LOW：nonce 必须真实出现在发网字节里——否则 `nonce` 字段
            # 与请求体解耦，「标记由 nonce 派生」的前提就不成立。
            nonce = str(rec.get("nonce") or "")
            if not nonce or nonce.encode("utf-8") not in plan.serialized:
                raise ProtocolBlocked(
                    "PREREG_MISMATCH",
                    f"slot {rec['slot']}#{rec['index']} nonce 未绑定在请求体字节内")
        # 元数据完整性（reviewer-r3 MAJOR M1）：只验请求体字节不够——篡改者可以
        # 重算 request_sha256，把长链件换成短请求并放宽 interval，整份 prereg 仍
        # 自洽。链位（slot/kind/index）、每槽条数与长链目标区间一律与**代码常量**
        # 比对（代码常量随实现面入 LOCK manifest），文件只作载体不作真值。
        expected: "list[tuple[int, str, str, int]]" = [
            (slot, kind, chain, idx)
            for slot, kind, chain, n in CHAIN
            for idx in range(1, n + 1)]
        if doc.get("post_count") not in (None, PROTOCOL_POSTS):
            raise ProtocolBlocked("PREREG_MISMATCH",
                                  f"post_count={doc.get('post_count')}")
        for rec, (slot, kind, chain, idx) in zip(posts, expected):
            try:
                got_pos = (int(rec.get("slot")), str(rec.get("kind")),
                           str(rec.get("chain")), int(rec.get("index")))
            except (TypeError, ValueError) as exc:
                raise ProtocolBlocked(
                    "PREREG_MISMATCH",
                    f"链位字段不可解析（{type(exc).__name__}: {exc}）") from exc
            # REVIEWER-R3 LOW：链标识也须绑定——只验 (slot,kind,index) 时，
            # 把 chain 改成任意串仍可通过。
            if got_pos != (slot, kind, chain, idx):
                raise ProtocolBlocked(
                    "PREREG_MISMATCH",
                    f"预登记链位不符：{got_pos} != {(slot, kind, chain, idx)}")
            want = (LONG_TARGETS[slot][idx - 2]
                    if kind == "long_context_tool_chain" and idx >= 2 else None)
            got = rec.get("interval")
            if want is None:
                if got not in (None, []):
                    raise ProtocolBlocked(
                        "PREREG_MISMATCH",
                        f"slot {slot}#{idx} 非长链件不得带区间：{got}")
            else:
                try:
                    got_iv = tuple(int(x) for x in got)
                except (TypeError, ValueError) as exc:
                    raise ProtocolBlocked(
                        "PREREG_MISMATCH",
                        f"slot {slot}#{idx} 区间不可解析（{exc}）") from exc
                if got_iv != want:
                    raise ProtocolBlocked(
                        "PREREG_MISMATCH",
                        f"slot {slot}#{idx} 区间 {got} 与代码常量 {list(want)} 不符")
        return doc

    # -- 单 POST ----------------------------------------------------------
    def _send(self, plan: T.SendPlan, *, slot_id: str, sender: T.Sender,
              accounting: T.Accounting) -> T.Attempt:
        try:
            return self.transport.send(plan, accounting, slot_id=slot_id,
                                       sender=sender)
        except T.PreflightRefused as exc:
            raise ProtocolBlocked("PREFLIGHT_BLOCKED", str(exc)) from exc

    def _account(self, plan: T.SendPlan, interval: "tuple[int, int] | None",
                 overhead: "tuple[int, int] | None") -> T.Accounting:
        return count_plan(plan, tokenizer=self.tokenizer, overhead=overhead,
                          interval=interval)

    def _claim(self, budget_key: str) -> None:
        if self.budget is not None:
            self.budget.claim(slot_id=budget_key, scope="protocol", family=None,
                              trial_id=None)

    def _record(self, budget_key: str, *, state: str) -> None:
        if self.budget is not None:
            self.budget.record_post(budget_key, state=state)

    def _attempts(self) -> "list[T.Attempt]":
        """读底层 Transport 已记录的 Attempt（PaidTransport 包裹时穿透）。"""
        tr = self.transport
        inner = getattr(tr, "inner", None) or getattr(tr, "_inner", None)
        return list(getattr(inner or tr, "attempts", []) or [])

    def _post(self, rec: Mapping[str, Any], *, slot_id: str, budget_key: str,
              sender: T.Sender,
              overhead: "tuple[int, int] | None" = None) -> "tuple[T.Attempt, T.Accounting]":
        plan = T.SendPlan(request=rec["request"],
                          max_output_tokens=int(rec["max_output_tokens"]))
        interval = tuple(rec["interval"]) if rec.get("interval") else None
        accounting = self._account(plan, interval, overhead)
        try:
            att = self._send(plan, slot_id=slot_id, sender=sender,
                             accounting=accounting)
        except ProtocolBlocked:
            raise
        except L.LockError as exc:
            # 付费闸拒绝发生在任何 POST 之前（reviewer-r3 M3 的反面保证）：
            # 不能记成「状态不确定」——零发送必须如实分类为审批/锁定类阻断。
            raise ProtocolBlocked(str(exc.code or "BLOCKED_APPROVAL"),
                                  f"{slot_id}#{rec['index']} LOCK 闸拒绝：{exc}")
        except Exception as exc:  # noqa: BLE001 —— 发送状态不确定：记账后即停，绝不重发
            last = None
            for cand in self._attempts():
                if cand.request_sha256 == plan.sha256:
                    last = cand
            if last is not None:
                # 先落「已发」事实再记账：即使槽账拒绝，报告里的 posts_sent 也必须
                # 反映真实发网次数（reviewer-r3 CRITICAL 的观测面）。
                self.posts.append({"slot": rec["slot"], "chain": rec["chain"],
                                   "index": rec["index"],
                                   "attempt": last.as_dict(),
                                   "accounting": accounting.as_dict(),
                                   "tokenizer_tokens": None,
                                   "interval": rec.get("interval")})
                self._record(budget_key, state=last.state)
            raise ProtocolBlocked(
                "CALL_FAILED",
                f"{slot_id}#{rec['index']} 发送状态不确定 "
                f"({type(exc).__name__}: {exc})；不重发") from exc
        tok_tokens = (self.tokenizer.count(render_text(plan))
                      if self.tokenizer is not None else None)
        self.posts.append({"slot": rec["slot"], "chain": rec["chain"],
                           "index": rec["index"],
                           "attempt": att.as_dict(),
                           "accounting": accounting.as_dict(),
                           "tokenizer_tokens": tok_tokens,
                           "interval": rec.get("interval")})
        self._record(budget_key, state=att.state)
        # 事后证伪：供应商实际 usage 必须落在发网前声明的计数界内。越界说明
        # 上界（或下界）不成立——那正是「长链长度/≤20000」两处声明的依据，
        # 因此按仪器故障停链，不把越界当噪声放过。
        usage_in = (att.usage.get("input_tokens")
                    if isinstance(att.usage, Mapping) else None)
        if att.state == T.SendState.SENT_OK and isinstance(usage_in, int):
            lo = (accounting.lower_tokens
                  if accounting.lower_tokens is not None else 0)
            if usage_in > accounting.upper_tokens or usage_in < lo:
                raise ProtocolBlocked(
                    "ACCOUNTING_MISMATCH",
                    f"{slot_id}#{rec['index']} 供应商 usage={usage_in} 落在"
                    f"计数界 [{accounting.lower_tokens},{accounting.upper_tokens}] 外")
        if att.state != T.SendState.SENT_OK:
            raise ProtocolBlocked("CALL_FAILED",
                                  f"{slot_id}#{rec['index']} state={att.state} "
                                  f"codes={att.reason_codes}")
        return att, accounting

    # -- 开销带 -----------------------------------------------------------
    def _overhead_band(self) -> "tuple[int, int]":
        """用前 6 个成功 POST 的 usage − 分词器计数给出供应商开销带。"""
        if self.tokenizer is None:
            raise ProtocolBlocked("BLOCKED_CONTEXT_MEASUREMENT", "无分词器")
        deltas: "list[int]" = []
        for row in self.posts:
            att = row["attempt"]
            if att["state"] != T.SendState.SENT_OK:
                continue
            usage = att.get("usage") or {}
            got = usage.get("input_tokens") if isinstance(usage, Mapping) else None
            if not isinstance(got, int):
                continue
            tok = row.get("tokenizer_tokens")
            if not isinstance(tok, int):
                tok = row["accounting"].get("lower_tokens")
            if not isinstance(tok, int):
                continue
            deltas.append(int(got) - int(tok))
        if len(deltas) < 6:
            raise ProtocolBlocked("BLOCKED_CONTEXT_MEASUREMENT",
                                  f"成功 usage 样本不足 6 个（{len(deltas)}）")
        lo, hi = min(deltas), max(deltas)
        if lo < 0:
            raise ProtocolBlocked(
                "BLOCKED_CONTEXT_MEASUREMENT",
                f"分词器低估供应商计数（delta 有负值 {lo}）：不能用作下界")
        if hi - lo > OVERHEAD_SPREAD_MAX:
            raise ProtocolBlocked(
                "BLOCKED_CONTEXT_MEASUREMENT",
                f"开销离散过大 {lo}..{hi} > {OVERHEAD_SPREAD_MAX}")
        return lo, hi

    # -- 回复捕获 ---------------------------------------------------------
    @staticmethod
    def _capturing(sender: T.Sender) -> "tuple[T.Sender, dict[str, tuple[Mapping[str, Any], str]]]":
        store: "dict[str, tuple[Mapping[str, Any], str]]" = {}

        def wrapped(plan: T.SendPlan):
            meta, text = sender(plan)
            store[plan.sha256] = (meta, text)
            return meta, text
        return wrapped, store

    # -- 三条链 -----------------------------------------------------------
    def _run_short(self, rec: Mapping[str, Any], *, slot_id: str,
                   budget_key: str, sender: T.Sender,
                   store) -> "dict[str, Any]":
        att, acc = self._post(rec, slot_id=slot_id, budget_key=budget_key,
                              sender=sender)
        text = store.get(att.request_sha256, ({}, ""))[1]
        parsed = parse_json_object(text)
        checks = {
            "text_non_empty": bool((text or "").strip()),
            "parseable_json_object": parsed is not None,
            "nonce_echoed": bool(parsed) and str(parsed.get("nonce")) == rec["nonce"],
            "stop_reason_recorded": bool(att.stop_reason),
            "usage_recorded": isinstance(att.usage, Mapping)
                              and isinstance(att.usage.get("input_tokens"), int),
        }
        if not all(checks.values()):
            raise ProtocolBlocked("SHORT_TEXT_FAILED",
                                  f"{slot_id} 校验未过：{checks}")
        return {"slot": rec["slot"], "chain": rec["chain"], "state": "PASS",
                "checks": checks, "request_sha256": att.request_sha256,
                "usage": att.usage}

    def _run_tool(self, recs: "list[Mapping[str, Any]]", *, slot_id: str,
                  budget_key: str, sender: T.Sender, store,
                  tool: ToolExec) -> "dict[str, Any]":
        first = recs[0]
        att1, _ = self._post(first, slot_id=slot_id, budget_key=budget_key,
                             sender=sender)
        parsed = parse_json_object(store.get(att1.request_sha256, ({}, ""))[1])
        action = (parsed or {}).get("action") or {}
        if not isinstance(action, Mapping) or action.get("tool") != "read_file":
            raise ProtocolBlocked("TOOL_ACTION_INVALID",
                                  f"{slot_id} 首轮未产出合法 read_file 动作：{parsed}")
        name = str(action.get("name") or "")
        if name != MARKER_NAME:
            raise ProtocolBlocked("TOOL_ACTION_INVALID",
                                  f"{slot_id} 动作文件名非法：{name!r}")
        expect = marker_for(first["nonce"])
        info = tool.prepare(name=name, content=expect)
        got = tool.read(name)
        if not got.get("executed_in_container"):
            raise ProtocolBlocked("TOOL_NOT_EXECUTED", f"{slot_id} 容器动作未执行")
        if got.get("sha256") != info["host_sha256"]:
            raise ProtocolBlocked(
                "TOOL_HASH_MISMATCH",
                f"{slot_id} 容器读到字节与宿主标记不同哈希")
        if str(got.get("content")) != expect:
            raise ProtocolBlocked("TOOL_CONTENT_MISMATCH",
                                  f"{slot_id} 容器内容与预登记标记不符")
        att2, _ = self._post(recs[1], slot_id=slot_id, budget_key=budget_key,
                             sender=sender)
        parsed2 = parse_json_object(store.get(att2.request_sha256, ({}, ""))[1])
        checks = {
            "action_valid": True,
            "tool_executed_in_container": True,
            "container_hash_matches": True,
            "second_round_nonce": bool(parsed2)
                                  and str(parsed2.get("nonce")) == first["nonce"],
            "second_round_value": bool(parsed2)
                                  and str(parsed2.get("value")) == expect,
            "second_round_sha256": bool(parsed2)
                                   and str(parsed2.get("sha256")) == got["sha256"],
        }
        if not all(checks.values()):
            raise ProtocolBlocked("TOOL_ROUNDTRIP_FAILED",
                                  f"{slot_id} 校验未过：{checks}")
        return {"slot": first["slot"], "chain": first["chain"], "state": "PASS",
                "checks": checks, "tool_event": got,
                "requests": [att1.request_sha256, att2.request_sha256]}

    def _run_long(self, recs: "list[Mapping[str, Any]]", *, slot_id: str,
                  budget_key: str, sender: T.Sender, store, tool: ToolExec,
                  overhead: "tuple[int, int]") -> "dict[str, Any]":
        first = recs[0]
        att1, _ = self._post(first, slot_id=slot_id, budget_key=budget_key,
                             sender=sender)
        parsed = parse_json_object(store.get(att1.request_sha256, ({}, ""))[1])
        action = (parsed or {}).get("action") or {}
        if not isinstance(action, Mapping) or action.get("tool") != "read_file":
            raise ProtocolBlocked("TOOL_ACTION_INVALID",
                                  f"{slot_id} 首轮未产出合法动作：{parsed}")
        # reviewer-r3 L6：与短链对齐——长链也必须点名预登记标记文件，否则
        # 模型可以要求读任意文件而仍被我们喂入标记内容。
        if str(action.get("name") or "") != MARKER_NAME:
            raise ProtocolBlocked(
                "TOOL_ACTION_INVALID",
                f"{slot_id} 首轮动作文件名 {action.get('name')!r} != {MARKER_NAME!r}")
        expect = marker_for(first["nonce"])
        info = tool.prepare(name=MARKER_NAME, content=expect)
        got = tool.read(MARKER_NAME)
        if got.get("sha256") != info["host_sha256"] or str(got.get("content")) != expect:
            raise ProtocolBlocked("TOOL_HASH_MISMATCH",
                                  f"{slot_id} 容器读取与预登记标记不符")
        rows: "list[dict[str, Any]]" = []
        checks: "dict[str, bool]" = {"stage1_action_valid": True,
                                     "tool_executed_in_container":
                                         bool(got.get("executed_in_container"))}
        for rec in recs[1:]:
            att, acc = self._post(rec, slot_id=slot_id, budget_key=budget_key,
                                  sender=sender, overhead=overhead)
            parsed_n = parse_json_object(store.get(att.request_sha256, ({}, ""))[1])
            key = f"stage{rec['index']}_"
            # 目标区间取代码常量而不取文件字段（MAJOR M1 的纵深防御：load_prereg
            # 已要求文件与常量相等，这里再以常量为准，文件篡改不可能放宽判据）。
            target = LONG_TARGETS[int(first["slot"])][int(rec["index"]) - 2]
            inside = (target is not None
                      and acc.lower_tokens is not None
                      and acc.upper_tokens is not None
                      and acc.lower_tokens >= target[0]
                      and acc.upper_tokens <= target[1])
            checks.update({
                key + "interval_inside": bool(inside),
                key + "nonce": bool(parsed_n)
                               and str(parsed_n.get("nonce")) == first["nonce"],
                key + "value": bool(parsed_n)
                               and str(parsed_n.get("value")) == expect,
                key + "sha256": bool(parsed_n)
                                and str(parsed_n.get("sha256")) == got["sha256"],
            })
            rows.append({"index": rec["index"], "request_sha256": att.request_sha256,
                         "usage": att.usage, "interval": rec.get("interval"),
                         "lower_tokens": acc.lower_tokens,
                         "upper_tokens": acc.upper_tokens})
        if not all(checks.values()):
            raise ProtocolBlocked("LONG_CHAIN_FAILED",
                                  f"{slot_id} 校验未过：{checks}")
        return {"slot": first["slot"], "chain": first["chain"], "state": "PASS",
                "checks": checks, "tool_event": got, "requests": rows}

    # -- 主流程 -----------------------------------------------------------
    def run(self, *, sender: T.Sender, tool: "ToolExec | None" = None) -> "dict[str, Any]":
        prereg = self.load_prereg()
        tool = tool or ToolExec(root=self.tool_root)
        by_slot: "dict[int, list[Mapping[str, Any]]]" = {}
        for rec in prereg["posts"]:
            by_slot.setdefault(int(rec["slot"]), []).append(rec)
        wrapped, store = self._capturing(sender)
        blocked: "ProtocolBlocked | None" = None
        overhead: "tuple[int, int] | None" = None
        rows: "list[dict[str, Any]]" = []
        for slot, kind, chain, count in CHAIN:
            slot_id = f"protocol_{chain}"
            recs = sorted(by_slot.get(slot, []), key=lambda r: int(r["index"]))
            if blocked is not None:
                self.cancelled.append({"slot": slot, "chain": chain,
                                       "kind": kind, "state": "CANCELLED",
                                       "reason": blocked.code})
                rows.append({"slot": slot, "chain": chain, "kind": kind,
                             "state": "CANCELLED", "reason": blocked.code})
                continue
            bkey = budget_slot_id(slot_id, slot)
            try:
                # reviewer-r3 MEDIUM：占用必须在 try 内——恢复态下 SlotExhausted
                # 原先会直接逃出 run()，既不写报告也不标余槽 CANCELLED。
                self._claim(bkey)
                if kind == "short_text":
                    row = self._run_short(recs[0], slot_id=slot_id,
                                          budget_key=bkey,
                                          sender=wrapped, store=store)
                elif kind == "tool_roundtrip":
                    if tool is None:
                        raise ProtocolBlocked("TOOL_UNAVAILABLE", "无工具执行器")
                    row = self._run_tool(recs, slot_id=slot_id, budget_key=bkey,
                                         sender=wrapped,
                                         store=store, tool=tool)
                else:
                    if overhead is None:
                        overhead = self._overhead_band()
                    if tool is None:
                        raise ProtocolBlocked("TOOL_UNAVAILABLE", "无工具执行器")
                    # reviewer-r3 L4：整槽先算计数再发第一件——若 stage3 区间
                    # 不可证，绝不能先花掉 stage1/2 两个 POST 才发现。
                    for rec in recs[1:]:
                        plan = T.SendPlan(
                            request=rec["request"],
                            max_output_tokens=int(rec["max_output_tokens"]))
                        pre = self._account(
                            plan,
                            LONG_TARGETS[int(rec["slot"])][int(rec["index"]) - 2],
                            overhead)
                        if pre.verdict != T.AccountingVerdict.OK:
                            code = ("TOKEN_OVER_CAP"
                                    if pre.verdict ==
                                    T.AccountingVerdict.TOKEN_OVER_CAP
                                    else "BLOCKED_CONTEXT_MEASUREMENT")
                            raise ProtocolBlocked(
                                code,
                                f"{slot_id} 预检 stage{rec['index']} 未过："
                                f"{pre.verdict} {list(pre.reasons)}")
                    row = self._run_long(recs, slot_id=slot_id, budget_key=bkey,
                                         sender=wrapped, store=store, tool=tool,
                                         overhead=overhead)
                row["kind"] = kind
                rows.append(row)
            except AT.SlotExhausted as exc:
                # 槽账拒绝是独立的阻断类（不与「未预料异常」混同）：零 POST，
                # 不补考，后续槽 CANCELLED。
                blocked = ProtocolBlocked(
                    "BLOCKED_BUDGET", f"{slot_id} 槽账拒绝：{exc}；不补考")
                rows.append({"slot": slot, "chain": chain, "kind": kind,
                             "state": "FAILED", "code": blocked.code,
                             "detail": blocked.detail})
            except ProtocolBlocked as exc:
                blocked = exc
                if self.budget is not None and exc.code in (
                        "PREFLIGHT_BLOCKED", "BLOCKED_APPROVAL", "LOCK_CHANGED"):
                    try:
                        self.budget.mark_preflight_blocked(bkey, reason=exc.code)
                    except Exception:  # noqa: BLE001 —— 记账失败不掩盖原始阻塞
                        pass
                rows.append({"slot": slot, "chain": chain, "kind": kind,
                             "state": "FAILED", "code": exc.code,
                             "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001 —— 未预料异常同样必须停链并留报告
                # reviewer-r3 M2：ToolError/SlotExhausted/IndexError 等非
                # ProtocolBlocked 异常原先会逃出 run()，导致既不写报告也不把
                # 余槽标 CANCELLED。此处一律收敛为 CALL_FAILED（失败即停）。
                blocked = ProtocolBlocked(
                    "CALL_FAILED", f"{slot_id} 未预料异常 "
                                   f"({type(exc).__name__}: {exc})；不重试")
                rows.append({"slot": slot, "chain": chain, "kind": kind,
                             "state": "FAILED", "code": blocked.code,
                             "detail": blocked.detail})
        report = {
            "schema": REPORT_SCHEMA,
            "checked_at_utc": C._utc_now(),
            "model": self.model,
            "verdict": "PASS" if blocked is None else "FAILED",
            "blocked_code": blocked.code if blocked else None,
            "blocked_detail": blocked.detail if blocked else None,
            "overhead_band": list(overhead) if overhead else None,
            "slots": rows,
            "posts_sent": len(self.posts),
            "posts": self.posts,
            "transport": (self.transport.summary() if self.transport else None),
            "note": ("12/12 PASS 只称「固定探针通过」，不宣称统计可靠率；"
                     "失败即停，后续 slot CANCELLED，不补考。"
                     "overhead_additive_assumption：开销带由前 6 个短/工具 POST 的"
                     "usage−分词器计数外推到 ~19k 输入，隐含 delta 与长度弱相关；"
                     "spread≤16 与事后 ACCOUNTING_MISMATCH 为该假设的两道兜底"
                     "（reviewer-r3 L5）。"),
        }
        C.write_json(self.ctx, self.report_relpath(), report)
        # reviewer-r3 M4：结案器读 `protocol/t6_protocol.json` 的 ``passed``，
        # 而人读报告在 analysis/…；两份同源派生，字段名不同处在此显式对接。
        C.write_json(self.ctx, "protocol/t6_protocol.json", {
            "schema": "simpraxis.rsi_r01.t6_protocol.v1",
            "checked_at_utc": report["checked_at_utc"],
            "passed": report["verdict"] == "PASS",
            "verdict": report["verdict"],
            "blocked_code": report["blocked_code"],
            "posts_sent": report["posts_sent"],
            "overhead_band": report["overhead_band"],
            "report_relpath": self.report_relpath(),
        })
        return report
