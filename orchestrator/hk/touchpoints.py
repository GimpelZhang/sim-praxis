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
           "HeadlessSessionRunner", "HeadlessActor",
           "TOUCHPOINT_INSTRUCTIONS", "REQUIRED_INPUT_KEYS"]

SCHEMA = "simpraxis.rsi_s1.touchpoint.v1"

CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
CLAUDE_SHARE = Path.home() / ".local" / "share" / "claude"

#: S2b F3：每触点请求面必需键清单——缺键结构性拒绝（防触点信息面缺陷第四例）。
#: 键名对应触点收到的 value 的顶层字段（由 driver/K8 装配）。
#: 注：K1 的 value 是 K0 输出（spec/registered/sha256 + _ctx），spec 内层含
#: conditions/plan_sha256；K8 的 value 是 confirm_result + results/nc_evidence 别名。
REQUIRED_INPUT_KEYS: "dict[str, tuple[str, ...]]" = {
    "propose_plan": ("spec",),
    "write_analysis": ("run_manifest_summary",),
    "submit_claim": ("nc_evidence", "results"),
    "submit_claim_repair": ("_repair", "confirm_result"),
    "diagnose_fail": ("human_notes", "claim"),
    # S2c 开放设计
    "propose_design": ("domain_brief",),
    "propose_design_repair": ("domain_brief", "previous_design", "gate_report"),
}

#: S2b 触点指令 v3（S2 v1 候选 V2 基础上：F1.3 信息面补全 + F2 交叉校验联动）。
TOUCHPOINT_INSTRUCTIONS: "dict[str, str]" = {
    "propose_plan": (
        "任务：根据 request.json 的 experiment_spec 提交 study_plan。\n"
        "response.json 必须含：conditions（逐字抄录 spec.conditions 列表）、"
        "n_block（整数）、study（A_N 或 B_N）、plan_sha256（逐字抄录 "
        "spec.plan_sha256）。\n"
        "另必须含 negative_control 对象：base、sham（两者都必须逐字取自 "
        "NC 槽的 candidates）、rationale（该扰动为何对等价指标**可达**"
        "且**亚阈**）、design_check（nc_toolkit 的 screen/design 输出摘要，"
        "含 predicted_effect 与 status）。\n"
        "NC 槽可能在 request 的 experiment_spec.negative_control_spec / "
        "spec.nc_slot，也可能在顶层 nc_slot；先定位它（allowed_diff / "
        "max_perturbation / delta / equivalence_metric / candidates）。\n"
        "流程：先运行 python3 nc_toolkit.py --help；把槽与候选条件写成 JSON，"
        "用 nc_toolkit.py screen 或 design 计算参数-指标可达性与亚阈余量。\n"
        "硬约束：结构化不可达的参数轴（例：condim=3、无扭转载荷时 torsional "
        "friction 对线位移）必须被 nc_toolkit 判为 degenerate，**不得提名**；"
        "退化的 [0,0] 对照不是阴性证据。若 design 返回 selected_unquantified，"
        "rationale 必须如实写“可达但幅度未量化，待 K5 复核”，不得声称已亚阈；"
        "若 request 无 NC 槽（写 NO_SLOT）或槽内没有可行候选（写具体原因），"
        "negative_control 置 null 并写 nc_blocked，不得凑一个 no-op。\n"
        "你只提名 base/sham；不得写权重或 δ（host 用冻结构造器生成对照），"
        "不得修改 conditions/n_block/plan_sha256；对规格有异议只写在 reason。"),
    "write_analysis": (
        "任务：写一份冻结分析程序 analysis_program。\n"
        "response.json 必须含 source 字段（Python 源码字符串）：从 request.json 的 "
        "run_manifest_summary 给出的记录路径出发，逐 block 计算线性对比值并给出 "
        "bootstrap 区间；若 run_manifest_summary 带 negative_control 对照，"
        "用同一实现同时输出其逐 block 值。\n"
        "宿主会用冻结方法独立重算并执行 NC 三连判据（非退化/亚阈/灵敏度校准）；"
        "你的程序作为证据留档，不承担判据。"),
    "submit_claim": (
        "任务：对 request.json 的 confirm_result 提交 claim。\n"
        "response.json 必须含：verdict、statement（一句话结论）、contrasts"
        "（逐对比行，逐字复制 request.results 的 "
        "id/metric_id/proposition/delta/estimate/ci_low/ci_high/n_blocks/claim "
        "九个字段）、report（六段对象，见下）。\n"
        "verdict 必须逐字等于 report/confirm_result 中**主对比**（results[0]）的 "
        "claim 字段（host 的 v2 分类）；不得自选。\n"
        "v2 五值词表（语义互斥，禁止混用）：SUPPORTED=方向主张被支持且越过 δ；"
        "REFUTED=证据与主张方向相反且越过 δ；NOT_DETECTED=CI 跨零（效应不可检出，"
        "**不等于否证**，禁止写 REFUTED）；EQUIVALENT=CI 整段落进 ±δ 等效带；"
        "INCONCLUSIVE=其余（区间横跨 δ 边界、点区间、样本不足）。\n"
        "statement 必须与 verdict 同义：CI 跨零只能写“未检出/NOT_DETECTED”，"
        "不得写“否证/REFUTED”；主对比与阴性对照结论不同时分别陈述。\n"
        "report 必须直接以六个段名为键（不要再包一层 sections；每段非空、"
        "≥8 字符）：question_hypothesis（问题与可"
        "反驳假设）、methods（条件矩阵/配对区块/宿主私有 nuisance）、main_results"
        "（逐对比 estimate/CI/分类）、heterogeneity_scope（结论能覆盖到哪里）、"
        "limitations_next_steps（局限与下一步）、negative_control_handling"
        "（阴性对照处理）。\n"
        "**request.json 的 nc_evidence 字段提供了 NC 证据**（NC 注册行 + 三连判据"
        "结果 + estimate/CI）。negative_control_handling 段必须**如实引用**这些"
        "证据：逐项写出 base/sham、**非退化**证据（逐 block 最大 |效应|、CI 宽度，"
        "不得全零）、**亚阈**证据（|estimate|<δ）、**灵敏度校准**（同管线注入已知"
        "效应可检出）。**禁止**写“未注册/未验证”——nc_evidence 在场时如此声称"
        "会被 G-REPORT_NC_MISMATCH 拒绝。\n"
        "数值必须与 host 复算逐字一致；任何转写错都会被 CLAIM_INVALID 拒绝；"
        "缺段或关键段无实质内容会被 G-REPORT 拒绝并给一次修复机会。"),
    "submit_claim_repair": (
        "任务：本轮 claim 被 G-REPORT 结构拒绝，要求**只做一次**修复后重交。\n"
        "request.json 的 _repair 给出 code、detail 与 missing（缺段清单）；"
        "request 只含 confirm_result，不含上次 claim 正文——请据 confirm_result "
        "完整重写 verdict/statement/contrasts/report。\n"
        "response.json 形状与 submit_claim 相同：verdict、statement、contrasts、"
        "report。修复规则：\n"
        "1) 按 missing 补写六段中缺的段；按 detail 修复被点名的段（如 "
        "NC_HANDLING_UNANCHORED 要补非退化/亚阈/灵敏度校准的处理，"
        "NEXT_STEPS_UNANCHORED 要补局限与下一步，G-REPORT_NC_MISMATCH "
        "要按 nc_evidence 如实引用 NC 证据而非声称未注册）；\n"
        "2) verdict 与 contrasts 的数值/分类**不得改动**，必须仍逐字等于 host "
        "复算结果（改动数值会被 CLAIM_INVALID 拒绝）；\n"
        "3) 不得编造未被 request 提供的 NC 证据；request 的 nc_evidence 在场时"
        "必须如实引用；\n"
        "4) 二次仍不合规将被结构拒绝，不进入确认批。"),
    "diagnose_fail": (
        "任务：审阅人打回了本轮结果（见 request.json 的 human_notes 与 claim）。\n"
        "response.json 必须含：diagnosis（问题归因与下一步）、algorithm_patch"
        "（布尔：打回意见是否含实质性算法修补）。\n"
        "若打回涉及阴性对照退化、词表/叙事错配或报告缺段，在 diagnosis 中指明"
        "对应机制（NC 设计/分类绑定/报告合同），不要只复述审阅意见。"),
    # —— S2c 开放设计（Q 池）：actor 只拿领域简报自提假设/因子/NC ----
    "propose_design": (
        "任务：根据 request.json 的 domain_brief 设计一个实验。\n"
        "response.json 必须含：hypothesis（可反驳假设）、conditions（条件矩阵，"
        "每个 {id, factors, scene_params}，factors 的值必须取自简报 factor_axes 的 values、"
        "条件数 ≤ max_conditions）、primary_contrast（{id, treatment_high, treatment_low, "
        "control_high, control_low, proposition}）、negative_control（{base, sham}）、"
        "rationale（设计如何区分简报里的 ≥2 个竞争解释）。\n"
        "metric 必须取自简报 metrics。不得超出简报界（因子/指标/条件数），越界会被 G-DESIGN "
        "拒绝并给修复机会。竞争解释的区分方式是设计的核心——不能只求显著。"),
    "propose_design_repair": (
        "任务：上一版设计被 G-DESIGN 结构门拒绝（见 request.json 的 gate_report 公开部分）。\n"
        "response.json 形状与 propose_design 相同。按 gate_report.code 修复："
        "DESIGN_OUT_OF_SPACE→收进界内；DESIGN_SHAPE_INVALID→减条件/换格；"
        "DESIGN_NC_INVALID→换有效 base/sham；DESIGN_PILOT_WEAK→加大主对比分辨力；"
        "DESIGN_NON_DISCRIMINATIVE→让设计更区分竞争解释。最多一次修复即可再交，二次仍不过"
        "即 DESIGN_NOT_READY 终态。不得靠猜参数或反复试到显著。"),
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
                 timeout_s: int = 900, max_turns: int = 40,
                 instructions: "Mapping[str, str] | None" = None,
                 toolkit_path: Path | None = None):
        self.sessions_root = Path(sessions_root)
        ensure_private_dir(self.sessions_root, mode=0o700)
        self.ledger = ledger
        self.claude_bin = Path(claude_bin or CLAUDE_BIN)
        self.claude_share = Path(claude_share or CLAUDE_SHARE)
        self.timeout_s = int(timeout_s)
        self.max_turns = int(max_turns)
        # S2 变异面：v1 臂的触点模板由**候选包**提供（candidate/touchpoints.py 的
        # TOUCHPOINT_INSTRUCTIONS_V2）；维护侧只提供注入点（v0 臂用 S1 原文）。
        self.instructions = dict(instructions or TOUCHPOINT_INSTRUCTIONS)
        # S2 变异面：v1 臂把候选的 NC 设计助手（nc_toolkit.py）放进 K1 工作区。
        self.toolkit_path = Path(toolkit_path) if toolkit_path else None

    # -- 沙箱 ------------------------------------------------------------
    def _command(self, workspace: Path, home: Path, touchpoint: str) -> list[str]:
        brief = _FIXED_BRIEF.format(
            touchpoint=touchpoint, workspace=str(workspace),
            instructions=self.instructions[touchpoint])
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
        if touchpoint not in self.instructions:
            raise TouchpointError("UNKNOWN_TOUCHPOINT", touchpoint)
        ws = ensure_private_dir(Path(workspace), mode=0o700)
        home = ensure_private_dir(ws / "home", mode=0o700)
        atomic_write_text(ws / "request.json",
                          json.dumps(dict(request), ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n", mode=0o600)
        atomic_write_text(ws / "INSTRUCTIONS.txt",
                          self.instructions[touchpoint] + "\n", mode=0o600)
        if touchpoint == "propose_plan" and self.toolkit_path is not None:
            # 候选的 NC 设计助手（变异面代码）随 K1 工作区进入沙箱；
            # 只读副本（0600），宿主不写候选内容。
            atomic_write_text(ws / "nc_toolkit.py",
                              self.toolkit_path.read_text(encoding="utf-8"),
                              mode=0o600)
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
        except BaseException as exc:  # 评审 MEDIUM：任何异常都必须结账，不留悬挂会话
            try:
                self.ledger.finish(session_id=session_key,
                                   usage={"status": "UNKNOWN",
                                          "error": f"{type(exc).__name__}"},
                                   exit_status="ERROR")
            except Exception:  # noqa: BLE001 - 结账失败不掩盖原始异常
                pass
            raise TouchpointError("SESSION_LAUNCH_FAILED",
                                  f"{type(exc).__name__}: {exc}") from None
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
        "submit_claim_repair": ("verdict", "statement", "contrasts"),
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
        # S2b F3：触点请求面清单——发请求前校验必需键，缺键结构性拒绝。
        required_keys = REQUIRED_INPUT_KEYS.get(name, ())
        missing_keys = [k for k in required_keys if k not in ctx]
        if missing_keys:
            raise TouchpointError(
                "REQUEST_INCOMPLETE",
                f"触点 {name} 请求缺必需键 {missing_keys}（REQUIRED_INPUT_KEYS）")
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
