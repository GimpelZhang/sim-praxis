"""S1-4 System 2 触点执行器（fresh headless 会话 + 会话账）。

口径（计划 S1-4 / 残尾③）：

* actor = **fresh** ``claude -p`` 会话，跑在 bwrap 沙箱：fresh HOME、只挂工作区 +
  系统目录 + claude 运行时；**不挂 /mnt/venvs**（沙箱内 mujoco 不可导入，负控见
  ``physics_service.mujoco_denial_probe``）、隐藏材料不可见（工作区外一律不挂）。
* 每个触点：``request.json`` 进、``response.json`` 出；transcript 全量落证据根；
  产者标记 = transcript init 事件的 session_id + model（模型自述无效）。
* 会话账：每次起止各写一行到 ``SessionLedger``（外耗面；与网关账分列不混算）。
* 无代写：actor 输出只来自被测会话；宿主只做协议校验（形状/条件漂移/转写一致），
  不补写内容。会话失败/缺 response 一律诚实失败（``TouchpointError``）。

触点预算由 driver 计（K1/K4/K8 + diagnose ≤2），本模块只做执行与记账。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir

from ..rsi_bridge.session_ledger import SessionLedger, SessionLedgerError

__all__ = ["SCHEMA", "TouchpointError", "TouchpointRecord",
           "HeadlessSessionRunner", "HeadlessActor", "TOUCHPOINT_INSTRUCTIONS"]

SCHEMA = "simpraxis.rsi_s1.touchpoint.v1"

CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
CLAUDE_SHARE = Path.home() / ".local" / "share" / "claude"

#: 固定触点说明（内容不随 run 变；不写死任何结果）。
TOUCHPOINT_INSTRUCTIONS: "dict[str, str]" = {
    "propose_plan": (
        "任务：根据 request.json 的 experiment_spec 提交 study_plan。\n"
        "response.json 必须含：conditions（逐字抄录 spec.conditions 列表）、"
        "n_block（整数）、study（A_N 或 B_N）、plan_sha256（逐字抄录 spec.plan_sha256）。\n"
        "不得修改条件或参数；若认为规格有缺陷，只能在 reason 字段说明。"),
    "write_analysis": (
        "任务：写一份冻结分析程序 analysis_program。\n"
        "response.json 必须含 source 字段（Python 源码字符串）：从 request.json 的 "
        "run_manifest 记录路径出发，逐 block 计算线性对比值并给出 bootstrap 区间。\n"
        "宿主会用冻结方法独立重算；你的程序作为证据留档，不承担判据。"),
    "submit_claim": (
        "任务：对 request.json 的 confirm_result 提交 claim。\n"
        "response.json 必须含：verdict（SUPPORTED/REFUTED/INCONCLUSIVE）、"
        "statement（一句话结论）、contrasts（逐对比行，逐字复制 "
        "id/estimate/ci_low/ci_high/n_blocks/claim 六个字段）。\n"
        "数值必须与 host 复算逐字一致；任何转写错都会被 CLAIM_INVALID 拒绝。"),
    "diagnose_fail": (
        "任务：审阅人打回了本轮结果（见 request.json 的 human_notes 与 claim）。\n"
        "response.json 必须含：diagnosis（问题归因与下一步）、algorithm_patch"
        "（布尔：打回意见是否含实质性算法修补）。"),
}

SUPPORTED_TOUCHPOINTS = tuple(TOUCHPOINT_INSTRUCTIONS)

_FIXED_BRIEF = """你是 RSI-S1 研究的独立执行会话（System 2 触点 {touchpoint}）。
工作目录 {workspace}。
1) 读取 {workspace}/request.json（JSON）。
2) 完成任务：
{instructions}
3) 把结果写成 {workspace}/response.json（UTF-8 JSON 对象）。
只读写当前工作目录内的文件；不要访问其它目录或隐藏材料。
"""


class TouchpointError(RuntimeError):
    """触点执行失败（会话崩溃/缺 response/协议形状不符）。带机器码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class TouchpointRecord:
    touchpoint: str
    session_key: str
    claude_session_id: str
    model: str
    workspace: Path
    transcript: Path
    usage: Mapping[str, Any]
    returncode: int
    elapsed_s: float
    started_at_utc: str
    response_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {"schema": f"{SCHEMA}.record", "touchpoint": self.touchpoint,
                "session_key": self.session_key,
                "claude_session_id": self.claude_session_id,
                "model": self.model, "workspace": str(self.workspace),
                "transcript": str(self.transcript), "usage": dict(self.usage),
                "returncode": self.returncode, "elapsed_s": self.elapsed_s,
                "started_at_utc": self.started_at_utc,
                "response_path": str(self.response_path)}


class HeadlessSessionRunner:
    """在 bwrap 里跑一个 fresh ``claude -p`` 会话并记账。"""

    def __init__(self, *, sessions_root: Path, ledger: SessionLedger,
                 claude_bin: Path | None = None,
                 claude_share: Path | None = None,
                 timeout_s: int = 900, max_turns: int = 40):
        self.sessions_root = Path(sessions_root)
        ensure_private_dir(self.sessions_root, mode=0o700)
        self.ledger = ledger
        self.claude_bin = Path(claude_bin or CLAUDE_BIN)
        self.claude_share = Path(claude_share or CLAUDE_SHARE)
        self.timeout_s = int(timeout_s)
        self.max_turns = int(max_turns)

    # -- 沙箱 ------------------------------------------------------------
    def _command(self, workspace: Path, home: Path, touchpoint: str) -> list[str]:
        brief = _FIXED_BRIEF.format(
            touchpoint=touchpoint, workspace=str(workspace),
            instructions=TOUCHPOINT_INSTRUCTIONS[touchpoint])
        inner = [str(self.claude_bin.resolve()), "-p", brief,
                 "--output-format", "stream-json", "--verbose",
                 "--max-turns", str(self.max_turns),
                 "--allowedTools", "Bash,Read,Write,Edit",
                 "--permission-mode", "acceptEdits"]
        return [
            "bwrap", "--die-with-parent", "--new-session",
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/etc", "/etc",
            "--dir", "/run",
            "--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve",
            "--ro-bind", str(self.claude_share), str(self.claude_share),
            "--bind", str(workspace), str(workspace),
            "--bind", str(home), "/home/s1actor",
            "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
            "--chdir", str(workspace),
            "--share-net",
            "--setenv", "HOME", "/home/s1actor",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            *inner,
        ]

    @staticmethod
    def _env() -> dict[str, str]:
        keep = {k: v for k, v in os.environ.items()
                if k.startswith("ANTHROPIC_") or k in ("LANG", "LC_ALL", "TERM")}
        keep["HOME"] = "/home/s1actor"
        return keep

    # -- 执行 ------------------------------------------------------------
    def run(self, *, touchpoint: str, workspace: Path,
            request: Mapping[str, Any], host_label: str = "") -> TouchpointRecord:
        if touchpoint not in SUPPORTED_TOUCHPOINTS:
            raise TouchpointError("UNKNOWN_TOUCHPOINT", touchpoint)
        ws = ensure_private_dir(Path(workspace), mode=0o700)
        home = ensure_private_dir(ws / "home", mode=0o700)
        atomic_write_text(ws / "request.json",
                          json.dumps(dict(request), ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", mode=0o600)
        atomic_write_text(ws / "INSTRUCTIONS.txt",
                          TOUCHPOINT_INSTRUCTIONS[touchpoint] + "\n", mode=0o600)
        command = self._command(ws, home, touchpoint)
        transcript = ws / "transcript.jsonl"
        session_key = f"tp-{touchpoint}-{uuid.uuid4().hex[:12]}"
        try:
            self.ledger.start(session_id=session_key, model="reported-after-start",
                              task=host_label or touchpoint, touchpoint=touchpoint)
        except SessionLedgerError as exc:
            raise TouchpointError(exc.code, exc.detail) from None
        started = utc_now_iso_z()
        t0 = time.monotonic()
        try:
            proc = subprocess.run(command, capture_output=True, text=True,
                                  timeout=self.timeout_s, env=self._env())
        except subprocess.TimeoutExpired:
            self.ledger.finish(session_id=session_key,
                               usage={"status": "UNKNOWN", "timeout": True},
                               exit_status="TIMEOUT")
            raise TouchpointError("SESSION_TIMEOUT", f">{self.timeout_s}s") from None
        elapsed = time.monotonic() - t0
        atomic_write_text(transcript, proc.stdout, mode=0o600)
        atomic_write_text(ws / "session.stderr.txt", proc.stderr, mode=0o600)
        meta = self._parse_transcript(transcript, proc.stdout)
        usage = {"status": "REPORTED" if meta["usage"] else "UNKNOWN",
                 "claude_session_id": meta["session_id"],
                 "model": meta["model"], **dict(meta["usage"] or {})}
        self.ledger.finish(session_id=session_key, usage=usage,
                           exit_status=f"rc={proc.returncode}")
        if proc.returncode != 0:
            raise TouchpointError("SESSION_FAILED",
                                  f"rc={proc.returncode} stderr="
                                  f"{proc.stderr.strip()[:400]}")
        response = ws / "response.json"
        if not response.is_file():
            raise TouchpointError("RESPONSE_MISSING", f"{response}")
        return TouchpointRecord(
            touchpoint=touchpoint, session_key=session_key,
            claude_session_id=str(meta["session_id"] or ""),
            model=str(meta["model"] or ""), workspace=ws, transcript=transcript,
            usage=usage, returncode=proc.returncode, elapsed_s=round(elapsed, 2),
            started_at_utc=started, response_path=response)

    @staticmethod
    def _parse_transcript(path: Path, stdout: str) -> dict[str, Any]:
        session_id = None
        model = None
        usage: dict[str, Any] | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("session_id"):
                session_id = event.get("session_id") or session_id
                model = event.get("model") or model
            message = event.get("message") or {}
            got = message.get("usage") or event.get("usage")
            if isinstance(got, dict):
                usage = {k: got.get(k) for k in
                         ("input_tokens", "output_tokens",
                          "cache_read_input_tokens",
                          "cache_creation_input_tokens") if got.get(k) is not None}
        return {"session_id": session_id, "model": model, "usage": usage}


class HeadlessActor:
    """driver 的 actor 协议实现：每触点一个 fresh 会话 + 协议校验 + 证据落盘。"""

    #: 各触点 response 必填键（形状坏 = 诚实失败，不猜）。
    REQUIRED = {
        "propose_plan": ("conditions", "n_block", "study", "plan_sha256"),
        "write_analysis": ("source",),
        "submit_claim": ("verdict", "statement", "contrasts"),
        "diagnose_fail": ("diagnosis",),
    }

    def __init__(self, runner: HeadlessSessionRunner, *,
                 evidence_root: Path, host_label: str = ""):
        self.runner = runner
        self.evidence_root = Path(evidence_root)
        self.host_label = host_label

    def __call__(self, name: str, contract: str, value: Mapping[str, Any]):
        ctx = value if isinstance(value, Mapping) else {}
        run_ctx = ctx.get("_ctx") if isinstance(ctx.get("_ctx"), Mapping) else {}
        raw_run_dir = run_ctx.get("run_dir") or ctx.get("run_dir")
        if not raw_run_dir:
            raise TouchpointError("RUN_DIR_MISSING", "context 未提供 run_dir")
        run_dir = Path(str(raw_run_dir))
        if not run_dir.is_dir():
            raise TouchpointError("RUN_DIR_MISSING", f"{run_dir}")
        workspace = run_dir / f"touchpoint_{name}"
        record = self.runner.run(touchpoint=name, workspace=workspace,
                                 request={"contract": contract, "value": dict(value)},
                                 host_label=self.host_label)
        try:
            response = json.loads(record.response_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise TouchpointError("RESPONSE_MALFORMED", str(exc)) from None
        if not isinstance(response, dict):
            raise TouchpointError("RESPONSE_SHAPE", "response.json 不是对象")
        missing = [k for k in self.REQUIRED[name] if k not in response]
        if missing:
            raise TouchpointError("RESPONSE_INCOMPLETE", f"{name} 缺 {missing}")
        # 产者链留痕（session_id+model 来自 transcript init，不靠模型自述）
        atomic_write_text(
            run_dir / f"touchpoint_{name}.meta.json",
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2,
                       sort_keys=True) + "\n", mode=0o600)
        return response
