"""T5 协议六次探针（AC25）：P1–P4 串行、无重试、真实发网、逐请求记账。

判据（计划 §3.5）：

===========  ======  ======================================================
试验          请求帽  成功条件
===========  ======  ======================================================
P1 / P2      各 2    非空可解析正文、终止类型与 usage 记录；不用 HTTP 200 顶替
P3 / P4      各 2    第一次请求产出合法动作 → 工具在候选沙盒读取运行时随机标记文件
                     → 第二次请求准确回报该标记，且记录**实际执行事件**
===========  ======  ======================================================

四条设计约束写进代码而不是写进纪律：

* **至多 6 个真实请求**：每个试验 `TRIAL_CAP=2`，整批 `TOTAL_CAP=6`，先查后发。
* **无重试**：网关以 ``max_attempts=1`` 构造（R0 组合根），单逻辑调用至多 1 次 POST；
  报告逐请求记录 ``attempts_http``，>1 即判违规。
* **逐请求记账**：每个请求先 `AttemptJournal.reserve()`（AC08 预留先于发网），成功后用
  账本里那一行的 `seq` 落 `mark_sent`/`settle_ok`；若网关在发网前就拒（无整数 seq 的
  账行），落 `block_preflight`——**不把没发出去的请求记成发过**。
* **任一试验失败即 PROTOCOL_BLOCKED**，其余试验登记 `CANCELLED_BY_GATE`（未发请求也留痕，
  不补抽到通过）；换模型/改协议/抬帽都不是本模块能做的事。

工具动作面只有 ``read_file``，路径必须在试验专属 workdir 内；标记由宿主 ``os.urandom``
生成（非秘密测试数据，不含任何凭据），宿主在容器外独立核对该内容。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator.common import runtime_paths as rp
from orchestrator.gateway import quota_ledger as ql
from orchestrator.gateway.quota_ledger import billing_context
from orchestrator.rsi_r0 import budget_contract
from orchestrator.rsi_r0.attempts import AttemptJournal, BudgetReservations
from orchestrator.rsi_r0.contracts import ResourceCaps, sha256_bytes, sha256_of
from orchestrator.rsi_r0.image_pin import IMAGE_REF

REPORT_SCHEMA = "rsi_r0.protocol_probe_report.v1"
PROTOCOL_BATCH = "R0-P"
PROTOCOL_TASK = 0
STAGE = "coding_debug"
TRIAL_CAP = 2
TOTAL_CAP = 6
TRIALS = ("P1", "P2", "P3", "P4")
REQUEST_TIMEOUT_S = 180          # §6.2：单请求超时帽
TOOL_TIMEOUT_S = 120             # §6.2：单次工具命令帽
PLAIN_MAX_TOKENS = 256
ACTION_MAX_TOKENS = 256
ANSWER_MAX_TOKENS = 256

TOOL_SCRIPT = Path(__file__).with_name("probe_tool.py")
WORK_MOUNT = "/work"
MARKER_NAME = "marker.txt"

#: 协议本体（§3.1：工具解析/上下文/重试/错误回传策略全部入 LOCK）。
PROTOCOL_SPEC: Mapping[str, Any] = {
    "schema": "rsi_r0.text_protocol.v1",
    "response_format": "single_line_json_object",
    "trial_order": list(TRIALS),
    "trial_request_cap": TRIAL_CAP,
    "total_request_cap": TOTAL_CAP,
    "retry": "none",
    "context_compression": "none",
    "error_feedback": "class_only_once",
    "tool_whitelist": ["read_file"],
    "tool_path_scope": WORK_MOUNT,
    "marker_source": "host_os_urandom_non_secret",
    "thinking": "not_requested",
    "temperature": 0.2,
}


def protocol_spec_hash() -> str:
    return sha256_of(dict(PROTOCOL_SPEC))


class ProbeBlocked(RuntimeError):
    """协议探针失败：PROTOCOL_BLOCKED（不自动切模型/协议/帽）。"""


class ProbeError(RuntimeError):
    """探针前置条件不成立（不属于协议结论，属于没资格跑）。"""


# ---------------------------------------------------------------- 提示词
def plain_prompt(nonce: str) -> str:
    return ("你正在参加一次文本协议探针。只输出一行 JSON，不要解释、不要代码块围栏：\n"
            '{"kind":"ready","nonce":"<NONCE>"}\n'
            f"其中 <NONCE> 必须原样填 {nonce}")


def action_prompt() -> str:
    return ("你正在参加一次工具往返探针。你需要读取一个文件，然后我会把文件内容回给你。\n"
            "现在只输出一行 JSON 动作，不要解释、不要代码块围栏：\n"
            '{"action":"read_file","path":"/work/' + MARKER_NAME + '"}\n'
            "除此之外的动作与路径一律非法。")


def marker_prompt(content: str) -> str:
    return ("工具 read_file 的结果如下（非秘密测试数据）：\n"
            f"{content}\n"
            "只输出一行 JSON，不要解释、不要代码块围栏：\n"
            '{"kind":"marker","value":"<你从工具结果里读到的内容>"}')


# ---------------------------------------------------------------- 解析
def parse_single_json(text: str) -> dict[str, Any] | None:
    """严格单行 JSON 对象；带围栏/多行/非对象一律 None（不宽容解析）。"""
    if not text or not text.strip():
        return None
    stripped = text.strip()
    if "\n" in stripped or stripped.startswith("```"):
        return None
    try:
        doc = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def validate_action(action: Mapping[str, Any] | None, *, work: Path) -> str:
    """动作白名单校验：只有 read_file，且路径解析后仍在本次试验 workdir 内。"""
    if action is None:
        raise ProbeBlocked("动作不是单行 JSON 对象")
    if action.get("action") != "read_file":
        raise ProbeBlocked(f"动作不在白名单：{action.get('action')!r}")
    raw = action.get("path")
    if not isinstance(raw, str) or not raw:
        raise ProbeBlocked("动作缺 path")
    name = os.path.basename(raw)
    if name != MARKER_NAME:
        raise ProbeBlocked(f"路径越界或非白名单文件：{raw!r}")
    target = (work / name).resolve()
    if work.resolve() not in target.parents:
        raise ProbeBlocked(f"路径逃出试验 workdir：{raw!r}")
    return name


# ---------------------------------------------------------------- 容器内工具
def _docker_argv(work: Path) -> list[str]:
    return [
        "docker", "run", "--rm",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=64",
        "--memory=512m",
        "--cpus=1",
        "--read-only",
        "--tmpfs=/tmp:rw,size=16m",
        "--user=1000:1000",
        "-v", f"{TOOL_SCRIPT}:/svc/probe_tool.py:ro",
        "-v", f"{work}:{WORK_MOUNT}:ro",
        IMAGE_REF, "python3", "/svc/probe_tool.py", f"{WORK_MOUNT}/{MARKER_NAME}",
    ]


def run_tool_in_container(*, work: Path, timeout_s: int = TOOL_TIMEOUT_S) -> dict[str, Any]:
    """在候选沙盒（固定 digest 镜像、禁网、非 root、只读挂载）里执行一次 read_file。"""
    if shutil.which("docker") is None:
        raise ProbeBlocked("docker 不可用：工具动作必须在候选沙盒容器内执行")
    argv = _docker_argv(work)
    started = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise ProbeBlocked(f"工具执行超时 {timeout_s}s（作废，不重试）") from exc
    wall = round(time.time() - started, 3)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("TOOL_JSON ")), None)
    if line is None:
        raise ProbeBlocked(
            f"容器未回报 TOOL_JSON（rc={proc.returncode}，stderr 前 200 字："
            f"{proc.stderr[:200]!r}）")
    payload = json.loads(line[len("TOOL_JSON "):])
    return {"argv_sha256": sha256_of(argv), "rc": proc.returncode,
            "wall_s": wall, "payload": payload}


# ---------------------------------------------------------------- 探针主体
class ProtocolProbe:
    def __init__(self, runner: Any, gateway: Any, *,
                 journal: AttemptJournal | None = None,
                 tool_runner: Callable[..., dict[str, Any]] | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        self.runner = runner
        self.gateway = gateway
        self.journal = journal or AttemptJournal(
            rp.rsi_r0_attempts_path(),
            BudgetReservations(ResourceCaps(), rp.rsi_r0_reservations_dir()))
        self.tool_runner = tool_runner or run_tool_in_container
        self.clock = clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.contract = budget_contract.read_contract(
            rp.rsi_r0_protocol_dir() / "pricing_contract.json")
        self._used = 0

    # ---- 前置 ----
    def preconditions(self) -> dict[str, Any]:
        ok, why = budget_contract.contract_ok(self.contract)
        if not ok:
            raise ProbeError(f"BLOCKED_COST：{why}（无最坏成本上界不许发网）")
        gate = self.runner.require_gate("G-P", batch_id=PROTOCOL_BATCH)   # 抛即拒
        try:
            ticket = ql.load_approval(PROTOCOL_BATCH, rp.rsi_r0_approvals_dir())
        except Exception as exc:  # noqa: BLE001 —— 无票据就是没资格跑
            raise ProbeError(f"批次票据不可用：{exc}") from exc
        if int(ticket.get("approved") or 0) > TOTAL_CAP:
            raise ProbeError(f"票据批准量 {ticket.get('approved')} 超过协议帽 {TOTAL_CAP}")
        model_id = self.gateway.model_id(STAGE) if hasattr(self.gateway, "model_id") else None
        return {"gate": gate, "ticket": ticket, "model_id": model_id,
                "pricing_contract_sha256": self.contract.get("contract_sha256")}

    # ---- 单请求 ----
    def _request(self, *, trial_id: str, index: int, prompt: str,
                 max_tokens: int) -> dict[str, Any]:
        if self._used >= TOTAL_CAP:
            raise ProbeError(f"已达整批请求帽 {TOTAL_CAP}，拒绝再发")
        if index > TRIAL_CAP:
            raise ProbeError(f"{trial_id} 已达本试验请求帽 {TRIAL_CAP}")
        cap_in = int(self.contract["input_tokens_cap"])
        cap_out = int(self.contract["output_tokens_cap"])
        per_usd = float(self.contract["worst_case_per_request_usd"])
        slot_id = f"{trial_id}-r{index}"
        rec = self.journal.reserve(slot_id=slot_id, batch_id=PROTOCOL_BATCH,
                                   task_id=PROTOCOL_TASK, stage=STAGE,
                                   tokens_in=cap_in, tokens_out=cap_out, usd=per_usd)
        # 顺序遵守 attempts 的崩溃契约：**先落 SENT 再发网**。这样进程若死在
        # HTTP 与结算之间，recover() 会看到 SENT ⇒ UNKNOWN_SENT（预留不释放、
        # 阻塞新付费批次），而不是误判成「从未发出」把可能已计费的一次放掉。
        # 发网前还不知道 seq；成功后用账本那一行的 seq 再落一条 SENT 事件补上。
        self.journal.mark_sent(rec, seq=None)
        before = len(ql.read_lines(rp.rsi_r0_ledger_path()))
        meta: dict[str, Any] = {}
        started = time.time()
        try:
            with billing_context(task=PROTOCOL_TASK, batch_id=PROTOCOL_BATCH,
                                 thread_id=f"protocol:{trial_id}"):
                text, usage = self.gateway.call(
                    STAGE, [{"role": "user", "content": prompt}],
                    max_tokens=max_tokens, temperature=0.2,
                    timeout=REQUEST_TIMEOUT_S, capture=meta)
        except Exception as exc:  # noqa: BLE001 —— 任何出口都必须落账，不许静默
            row = self._new_row(before)
            seq = row.get("seq") if row else None
            if isinstance(seq, int):
                self.journal.mark_sent(rec, seq=seq)
                self.journal.settle_fail(rec, error_class=type(exc).__name__)
            elif row is not None:
                # 账本留了 seq=null 行（凭据缺失/暂停/配额等发网前拦截）⇒ 可证实
                # 从未发网，按 BLOCKED_PREFLIGHT 结算（attempts_http 归 0）。
                self.journal.block_preflight(
                    rec, reason=f"gateway_refused:{type(exc).__name__}")
            else:
                # 既无整数 seq 也无 blocked 行：无法证明没发出去，保留最坏假设。
                self.journal.settle_fail(rec, error_class=f"unaccounted:{type(exc).__name__}")
            raise ProbeBlocked(
                f"{trial_id} 第 {index} 个请求失败：{type(exc).__name__}: {exc}") from exc

        row = self._new_row(before)
        if not row or row.get("ok") is not True or not isinstance(row.get("seq"), int):
            # 网关说成功却没留下一行 ok 的整数 seq 账 —— 记账缺口按阻断处理
            if row and isinstance(row.get("seq"), int):
                self.journal.mark_sent(rec, seq=row["seq"])
                self.journal.settle_fail(rec, error_class="ledger_row_not_ok")
            elif row is not None:
                self.journal.block_preflight(rec, reason="gateway_refused_no_seq")
            else:
                self.journal.settle_fail(rec, error_class="unaccounted:no_ledger_row")
            raise ProbeBlocked(f"{trial_id} 第 {index} 个请求：账本未留 ok 行（记账缺口）")

        self._used += 1
        attempts_http = meta.get("attempts_http")
        if isinstance(attempts_http, int) and attempts_http > 1:
            self.journal.mark_sent(rec, seq=row["seq"])
            self.journal.settle_fail(rec, error_class="HiddenRetryDetected")
            raise ProbeBlocked(
                f"{trial_id} 第 {index} 个请求实际发了 {attempts_http} 次（AC07 禁暗重试）")
        self.journal.mark_sent(rec, seq=row["seq"])
        self.journal.settle_ok(
            rec, usage_in=usage[0], usage_out=usage[1],
            stop_reason=meta.get("stop_reason"), block_types=("text",),
            provider_request_id=meta.get("provider_request_id"),
            body_sha256=meta.get("body_sha256"), usd=None)
        return {
            "index": index, "slot_id": slot_id, "request_id": rec.request_id,
            "seq": row["seq"], "text": text,
            "usage": {"in": usage[0], "out": usage[1]},
            "stop_reason": meta.get("stop_reason"),
            "provider_model": meta.get("provider_model"),
            "attempts_http": attempts_http,
            "body_sha256": meta.get("body_sha256"),
            "wall_s": round(time.time() - started, 3),
        }

    @staticmethod
    def _new_row(before: int) -> dict[str, Any] | None:
        rows = ql.read_lines(rp.rsi_r0_ledger_path())
        return rows[-1] if len(rows) > before else None

    # ---- 四个试验 ----
    def trial_plain(self, trial_id: str) -> dict[str, Any]:
        nonce = os.urandom(8).hex()
        req = self._request(trial_id=trial_id, index=1,
                            prompt=plain_prompt(nonce), max_tokens=PLAIN_MAX_TOKENS)
        parsed = parse_single_json(req["text"])
        checks = {
            "non_empty": bool(req["text"].strip()),
            "parseable_json_object": parsed is not None,
            "nonce_echoed": bool(parsed) and str(parsed.get("nonce")) == nonce,
            "stop_reason_recorded": bool(req["stop_reason"]),
            "usage_recorded": req["usage"]["in"] is not None and req["usage"]["out"] is not None,
            "one_post": req["attempts_http"] == 1,
        }
        return _trial_row(trial_id, checks, {"requests": [req], "nonce": nonce})

    def trial_tool(self, trial_id: str) -> dict[str, Any]:
        work = self.runner.trial_dir(f"protocol_{trial_id}") / "work"
        work.mkdir(parents=True, exist_ok=True)
        os.chmod(work, 0o700)
        marker = os.urandom(16).hex()
        marker_path = work / MARKER_NAME
        marker_path.write_text(marker + "\n", encoding="utf-8")
        os.chmod(marker_path, 0o600)
        # 宿主侧独立核对用的哈希必须与容器读到的字节**同一口径**：容器按二进制
        # 读整份文件再 sha256（含行尾换行），宿主也照此计算，否则两值永远不等。
        host_marker_sha = sha256_bytes(marker_path.read_bytes())

        first = self._request(trial_id=trial_id, index=1,
                              prompt=action_prompt(), max_tokens=ACTION_MAX_TOKENS)
        action = parse_single_json(first["text"])
        name = validate_action(action, work=work)          # 越界/非白名单即 ProbeBlocked
        outcome = self.tool_runner(work=work)
        payload = outcome.get("payload") or {}
        executed = bool(payload.get("ok"))
        observed = (payload.get("content") or "").strip()
        event = {
            "trial": trial_id, "action": dict(action or {}),
            "validated_path": f"{WORK_MOUNT}/{name}",
            "argv_sha256": outcome.get("argv_sha256"),
            "rc": outcome.get("rc"), "wall_s": outcome.get("wall_s"),
            "executed_in_container": executed,
            "container_seen_sha256": payload.get("content_sha256"),
            "host_marker_sha256": host_marker_sha,
            "content_matches_marker": executed and observed == marker,
            "recorded_at_utc": self.clock(),
        }
        if not event["content_matches_marker"]:
            raise ProbeBlocked(
                f"{trial_id} 工具执行未读到本试验标记（executed={executed}）")

        second = self._request(trial_id=trial_id, index=2,
                               prompt=marker_prompt(observed), max_tokens=ANSWER_MAX_TOKENS)
        parsed = parse_single_json(second["text"])
        checks = {
            "action_valid": True,
            "tool_executed_in_container": executed,
            "marker_matched_in_container": event["content_matches_marker"],
            "marker_echoed_by_model": bool(parsed) and parsed.get("kind") == "marker"
                                      and str(parsed.get("value")) == marker,
            "stop_reason_recorded": bool(second["stop_reason"]),
            "usage_recorded": all(r["usage"][k] is not None for r in (first, second)
                                  for k in ("in", "out")),
            "one_post": first["attempts_http"] == 1 and second["attempts_http"] == 1,
        }
        return _trial_row(trial_id, checks,
                          {"requests": [first, second], "tool_event": event,
                           "workdir": str(work)})

    # ---- 全批 ----
    def run(self) -> dict[str, Any]:
        pre = self.preconditions()
        rows_before = len(ql.read_lines(rp.rsi_r0_ledger_path()))
        trials: list[dict[str, Any]] = []
        blocked: str | None = None
        for trial_id in TRIALS:
            if blocked is not None:
                trials.append({"trial_id": trial_id, "state": "CANCELLED_BY_GATE",
                               "checks": {}, "requests_used": 0})
                continue
            try:
                row = (self.trial_plain(trial_id) if trial_id in ("P1", "P2")
                       else self.trial_tool(trial_id))
            except ProbeBlocked as exc:
                blocked = f"{trial_id}: {exc}"
                trials.append({"trial_id": trial_id, "state": "PROTOCOL_BLOCKED",
                               "checks": {}, "reason": str(exc),
                               "requests_used": self._used})
                continue
            row["state"] = "PASS" if row["passed"] else "FAIL"
            trials.append(row)
            if not row["passed"]:
                blocked = f"{trial_id}: 检查未全过 {[k for k, v in row['checks'].items() if not v]}"

        rows_after = ql.read_lines(rp.rsi_r0_ledger_path())
        seqs = [r.get("seq") for r in rows_after[rows_before:] if isinstance(r.get("seq"), int)]
        passed = all(t.get("state") == "PASS" for t in trials) and len(trials) == len(TRIALS)
        report = {
            "schema": REPORT_SCHEMA,
            "campaign": str(self.runner.root),
            "created_at_utc": self.clock(),
            "verdict": "PROTOCOL_OK" if passed else "PROTOCOL_BLOCKED",
            "blocked_reason": blocked,
            "protocol_spec": dict(PROTOCOL_SPEC),
            "protocol_spec_sha256": protocol_spec_hash(),
            "model": {"stage": STAGE, "resolved_model_id": pre["model_id"],
                      "image": IMAGE_REF},
            "gate": pre["gate"],
            "ticket": {k: pre["ticket"].get(k) for k in
                       ("batch_id", "task", "stage", "approved", "created_at_iso")},
            "pricing_contract_sha256": pre["pricing_contract_sha256"],
            "requests": {"cap_total": TOTAL_CAP, "cap_per_trial": TRIAL_CAP,
                         "used": self._used, "ledger_seqs": seqs},
            "trials": trials,
        }
        report["report_sha256"] = sha256_of(report)
        return report


def _trial_row(trial_id: str, checks: Mapping[str, bool],
               extra: Mapping[str, Any]) -> dict[str, Any]:
    return {"trial_id": trial_id, "checks": dict(checks),
            "passed": bool(checks) and all(checks.values()),
            "requests_used": len(extra.get("requests") or ()), **dict(extra)}


def write_report(report: Mapping[str, Any], path: Path | None = None) -> Path:
    target = path or (rp.rsi_r0_protocol_dir() / "protocol_probe_report.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                      + "\n", encoding="utf-8")
    return target
