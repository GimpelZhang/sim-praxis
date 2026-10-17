#!/usr/bin/env python3
"""Task4 agent_runner：真实脚本执行 + coding_debug 多轮补救（计划 L496-507）。

契约要点：
- 每 run 最多 5 轮 coding_debug（D10 v1.1）；第 5 次调用后仍败 → 写
  FAILURE.json 带完整轨迹，run 结案转人工；结构上不可能发出第 6 次调用。
- 检索 troubleshooting top_k=6，取行字段 **text**（非幻影字段 solution_steps）
  + evidence_refs，prompt 以 [来源: kb_relpath#section_path] 列注。
- Gate2 打回意见与 traceback 一起进 prompt，意见标「最高优先级」。
- 补丁安全：只允许写 workdir 下运行目录；禁改仓库 tracked 文件、
  stage_models.yaml、00_env.sh、~/access；越界补丁拒绝且不发起重试。
- 视觉禁用：runner 构造的消息只含纯文本（测试断言无 image 类型）。
- coding_round 计数仅在单次 run 内有效；Gate2 reject 计数在图侧独立维护。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from memory_flywheel.recall import ProductionRecall, prompt_embedded_text
from memory_flywheel.vector_engine import VectorEngine
from orchestrator.common.callcache import CallReplay, request_fingerprint
from orchestrator.common.clock import utc_now_iso_z
from orchestrator.common.fsutil import atomic_write_text, chmod_keep_user_exec
from orchestrator.gateway.quota_ledger import (
    billing_context,
    phase1_root,
    set_current_evidence,
)
from orchestrator.gateway.router import (
    GatewayPausedError,
    ModelGateway,
    redact,
)

STAGE = "coding_debug"
TOP_K = 6
MAX_CODING_ROUNDS = 5          # D10 v1.1：每 run 最多 5 次 coding_debug
MAX_TOKENS = 8192
TEMPERATURE = 0.1
TB_TAIL_CHARS = 4096           # traceback 尾部 ≤4KB
SNIPPET_CTX_LINES = 8
SNIPPET_TOTAL_CHARS = 4096
RUN_TIMEOUT_S = 1800
FAILURE_JSON = "FAILURE.json"

# 禁改的字面文件名（任何目录层级都拒）
FORBIDDEN_BASENAMES = frozenset({"stage_models.yaml", "00_env.sh"})


class CodingLimitError(GatewayPausedError):
    """5 轮用尽/失败 run 被再次进入：等价硬停，不发网（计划 L505 ③）。

    父类 GatewayPausedError.__init__ 绑定 router 长熔断的 pause-state dict；
    这里只复用「可被同一硬停子句捕获、绝不发网」的语义，故越过它直接走
    RuntimeError 初始化，isinstance(x, GatewayPausedError) 仍然成立。
    """

    def __init__(self, message: str):
        RuntimeError.__init__(self, message)


class PatchSafetyError(RuntimeError):
    """补丁越界或无法安全应用：拒绝且不重试（计划 L505 ②）。"""


@dataclass
class RunSpec:
    thread_id: str
    workdir: Path
    cmd: list[str]
    telemetry_relpath: str                 # 成功产物（相对 workdir）
    batch_id: str
    # L-1：本 run 在 thread 内的稳定序号（与证据文件名 r{run_idx} 同一约定，
    # 工厂用 len(agent_runs)+1 预占、接缝用 len(agent_runs) 复核，二者同号）。
    # 仅当显式给出时启用调用级重放缓存；None（如 Task4 直连）不查不写缓存。
    run_idx: Optional[int] = None
    billing_task: int = 4                  # Task7/8 联调时传 7/8（锁内适配 L506）
    gate2_notes: Optional[str] = None      # 图侧 gate2_rejected 事件注入
    env_extra: dict[str, str] = field(default_factory=dict)
    run_timeout_s: int = RUN_TIMEOUT_S
    task_title: str = ""
    # Task7：真实 rollout 视频产物（相对 workdir）。存在才回填视频四字段；
    # duration_ms 优先读场景 manifest（n_frames×frame_dt_s），缺失再 imageio 解码。
    video_relpath: Optional[str] = None
    cover_relpath: Optional[str] = None
    manifest_relpath: Optional[str] = None


@dataclass
class _ExecResult:
    rc: int
    stdout: str
    stderr: str


def _default_evidence_root(thread_id: str) -> Path:
    d = phase1_root() / "task4_runner" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    return d


SubprocessRunner = Callable[[list[str], Path, float, dict], _ExecResult]


def _default_subprocess(cmd, workdir, timeout_s, env_extra) -> _ExecResult:
    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.run(
        cmd, cwd=str(workdir), env=env, capture_output=True, text=True,
        timeout=timeout_s)
    return _ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# ---------------------------------------------------------------------------
# traceback 取证（只准读 workdir 内的帧文件）
# ---------------------------------------------------------------------------

_FRAME_RE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')


def _collect_snippets(workdir: Path, text: str) -> list[dict]:
    workdir_r = workdir.resolve()
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    total = 0
    for m in _FRAME_RE.finditer(text):
        fp = Path(m.group("path"))
        try:
            resolved = fp.resolve(strict=False)
            resolved.relative_to(workdir_r)
        except (OSError, ValueError):
            continue  # stdlib / site-packages / 仓库外：禁读
        key = (str(resolved), int(m.group("line")))
        if key in seen:
            continue
        seen.add(key)
        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        ln = int(m.group("line"))
        lo, hi = max(0, ln - 1 - SNIPPET_CTX_LINES), ln + SNIPPET_CTX_LINES
        block = "\n".join(f"{i+1:5d}| {lines[i]}" for i in range(lo, min(hi, len(lines))))
        if total + len(block) > SNIPPET_TOTAL_CHARS:
            break
        total += len(block)
        out.append({"file": str(resolved.relative_to(workdir_r)),
                    "line": ln, "snippet": block})
    return out


# ---------------------------------------------------------------------------
# 补丁解析与安全应用
# ---------------------------------------------------------------------------

def _validate_target(path: Path, workdir: Path) -> Path:
    name = path.name
    if name in FORBIDDEN_BASENAMES:
        raise PatchSafetyError(f"补丁企图修改禁改文件：{name}")
    if "access" in path.parts:
        raise PatchSafetyError(f"补丁企图进入 ~/access 区域：{path}")
    workdir_r = workdir.resolve()
    if path.is_absolute():
        target = path.resolve(strict=False)
    else:
        target = (workdir_r / path).resolve(strict=False)
    try:
        target.relative_to(workdir_r)
    except ValueError:
        raise PatchSafetyError(
            f"补丁目标越出 workdir：{path} -> {target}（workdir={workdir_r}）")
    # 拒绝 symlink 逃逸（路径上任何一环是指向 workdir 外的链接）
    cur = workdir_r
    for part in target.relative_to(workdir_r).parts:
        cur = cur / part
        if cur.is_symlink():
            real = cur.resolve(strict=False)
            try:
                real.relative_to(workdir_r)
            except ValueError:
                raise PatchSafetyError(f"补丁经符号链接逃逸 workdir：{cur} -> {real}")
    return target


_HUNK_RE = re.compile(
    r"^@@ -(?P<o>\d+)(?:,\d+)? \+(?P<n>\d+)(?:,\d+)? @@", re.M)


def _apply_unified_diff(workdir: Path, diff_text: str) -> list[str]:
    """最小 unified diff 应用器（git 风格头）；每段旧块必须唯一命中。"""
    files = re.split(r"^diff --git .+$", diff_text, flags=re.M)
    applied: list[str] = []
    for section in files:
        if not section.strip():
            continue
        m_old = re.search(r"^--- (?:a/)?(.+)$", section, re.M)
        m_new = re.search(r"^\+\+\+ (?:b/)?(.+)$", section, re.M)
        if not m_new:
            raise PatchSafetyError("unified diff 缺 +++ 头")
        rel = m_new.group(1).strip()
        target = _validate_target(Path(rel), workdir)
        body_start = section.find("\n@@")
        body = section[body_start + 1:] if body_start >= 0 else ""
        old_lines = target.read_text(encoding="utf-8").splitlines(keepends=True) \
            if target.exists() else []
        # 按 hunk 顺序定位替换
        result: list[str] = []
        cursor = 0
        hunks = list(_HUNK_RE.finditer(body))
        if not hunks:
            raise PatchSafetyError("unified diff 无 hunk")
        for hi, hm in enumerate(hunks):
            hunk_end = hunks[hi + 1].start() if hi + 1 < len(hunks) else len(body)
            hunk_lines = body[hm.start():hunk_end].splitlines()
            old_block, new_block = [], []
            for hl in hunk_lines[1:]:
                if hl.startswith("\\"):
                    continue
                if hl.startswith("-"):
                    old_block.append(hl[1:])
                elif hl.startswith("+"):
                    new_block.append(hl[1:])
                else:
                    ctx = hl[1:] if hl[:1] in (" ", "") else hl
                    old_block.append(ctx)
                    new_block.append(ctx)
            window = [l.rstrip("\n") for l in old_lines[cursor:]]
            old_s = [l.rstrip("\n") for l in old_block]
            hits = [i for i in range(len(window) - len(old_s) + 1)
                    if window[i:i + len(old_s)] == old_s]
            if len(hits) != 1:
                raise PatchSafetyError(
                    f"hunk 上下文命中 {len(hits)} 次（需恰好 1 次）：{rel}")
            at = cursor + hits[0]
            result.extend(old_lines[cursor:at])
            result.extend("\n".join(new_block).splitlines(keepends=True))
            cursor = at + len(old_s)
        result.extend(old_lines[cursor:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(result), encoding="utf-8")
        # L-11：收敛到属主 rw，但保留补丁目标文件原有的属主执行位（0700 脚本）。
        chmod_keep_user_exec(target)
        applied.append(str(target.relative_to(workdir.resolve())))
    return applied


def _apply_structured_patch(workdir: Path, patch: dict) -> str:
    rel = patch.get("path")
    if not isinstance(rel, str) or not rel:
        raise PatchSafetyError("补丁缺 path 字段")
    target = _validate_target(Path(rel), workdir)
    old, new = patch.get("old"), patch.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        raise PatchSafetyError("补丁 old/new 必须为字符串")
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    if text.count(old) != 1:
        raise PatchSafetyError(
            f"old 块命中 {text.count(old)} 次（需恰好 1 次）：{rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    # L-11：0700/0755 可执行脚本补丁后保留属主执行位，不再被抹成 0600。
    chmod_keep_user_exec(target)
    return str(target.relative_to(workdir.resolve()))


def _parse_and_apply(workdir: Path, raw: str) -> list[str]:
    obj = _json_object(raw)
    if not isinstance(obj, dict):
        raise PatchSafetyError("coding_debug 返回非 JSON 对象")
    if "unified_diff" in obj:
        diff = obj["unified_diff"]
        if not isinstance(diff, str) or not diff.strip():
            raise PatchSafetyError("unified_diff 为空")
        return _apply_unified_diff(workdir, diff)
    patches = obj.get("patches")
    if not isinstance(patches, list) or not patches:
        raise PatchSafetyError("返回中既无 unified_diff 也无 patches[]")
    applied = []
    for p in patches:
        if not isinstance(p, dict):
            raise PatchSafetyError("patches 元素必须是对象")
        applied.append(_apply_structured_patch(workdir, p))
    return applied


def _json_object(raw: str) -> Any:
    """容忍 ```json 围栏（与 Task2 parse_json_object 同风格的最小提取）。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

def _kb_block(hits: list[dict]) -> str:
    if not hits:
        return "（troubleshooting 检索 0 行；不得据此编造 KB 引用）"
    lines = []
    for i, h in enumerate(hits, 1):
        src = f"{h.get('kb_relpath') or '?'}#{h.get('section_path') or ''}"
        refs = h.get("evidence_refs") or []
        ref_str = (f" | evidence_refs: {', '.join(map(str, refs))}"
                   if refs else "")
        lines.append(f"[S{i} 来源: {src}{ref_str}]\n"
                     f"{(h.get('text') or '').strip()}")
    return "\n\n".join(lines)


def _learning_block(learning_hits: "list[dict] | None") -> str:
    """【实战演化避坑】段（Phase 2 Task1）：只渲染学习表真实行，逐字引用。

    空/None → ""（调用方整段不拼，0 命中时 prompt 不出现避坑标题；
    learning_hits=None 时消息与 Phase 1 一致，旧测试零改动）。补丁片段只能
    来自这些真实行的 text（含修复动作/人工意见原文），不得合成 KB 引用。
    """
    if not learning_hits:
        return ""
    lines = ["以下条目逐字摘自历史真实运行学习表 phase1_learnings，必须作为"
             "避坑约束对照；补丁只能引用这些真实条目，不得编造其中不存在的"
             "修复或 KB 引用："]
    for i, h in enumerate(learning_hits, 1):
        lines.append(
            f"[L{i} 来源 learning_id={h.get('id', '?')} "
            f"run_id={h.get('run_id', '?')} "
            f"gate_source={h.get('gate_source', '?')} "
            f"rung={h.get('rung', '?')}]\n{str(h.get('text', ''))}")
    return "\n\n".join(lines)


def _build_messages(*, gate2_notes: Optional[str], traceback_tail: str,
                    snippets: list[dict], hits: list[dict],
                    cmd: list[str],
                    learning_hits: "list[dict] | None" = None
                    ) -> list[dict[str, str]]:
    system = (
        "你是具身智能仿真工具链的排障 coding agent。只允许输出严格 JSON："
        '{"patches":[{"path":"相对运行工作目录的路径","old":"原文（须唯一）",'
        '"new":"替换文"}]}，或 {"unified_diff":"git 风格 diff"}。'
        "补丁只能改本次运行工作目录内的文件；禁止改配置、凭据、仓库源码。"
        "不要输出解释、不要输出 image。")
    notes_block = ""
    if gate2_notes and gate2_notes.strip():
        notes_block = (
            "## 人工评审意见（必须逐条响应，最高优先级）\n"
            f"{gate2_notes.strip()}\n")
    snippet_block = "\n\n".join(
        f"### {s['file']}:{s['line']}\n{s['snippet']}" for s in snippets) \
        or "（traceback 帧均在工作目录外，无源片段可读）"
    # Phase 2 Task1：避坑段仅在 learning 路有真实命中时出现，置于 kb 段之后；
    # 0 命中不拼标题（禁止据此编造 fix/KB 引用）。
    learning_section = ""
    learning_text = _learning_block(learning_hits)
    if learning_text:
        learning_section = "\n\n## 实战演化避坑（phase1_learnings 真实行，逐字引用）\n" \
            + learning_text
    user = (
        f"{notes_block}"
        "## 失败命令\n" + " ".join(cmd) + "\n\n"
        "## traceback / 日志尾部\n" + traceback_tail + "\n\n"
        "## 相关源文件片段（仅限运行工作目录）\n" + snippet_block + "\n\n"
        "## troubleshooting 知识库命中（namespaces=troubleshooting, top_k=6）\n"
        + _kb_block(hits) + learning_section)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _tb_tail(exec_result: _ExecResult) -> str:
    blob = (exec_result.stderr or "") + "\n" + (exec_result.stdout or "")
    return blob[-TB_TAIL_CHARS:]


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class AgentRunner:
    def __init__(self, gateway: ModelGateway, vector_engine: VectorEngine, *,
                 evidence_root: "Path | None" = None,
                 subprocess_runner: "SubprocessRunner | None" = None,
                 evidence_writer: "Callable | None" = None,
                 call_cache: "CallReplay | None" = None,
                 recall: "ProductionRecall | None" = None):
        self.gateway = gateway
        self.vector_engine = vector_engine
        self._evidence_root = evidence_root
        self._run = subprocess_runner or _default_subprocess
        self._write_evidence = evidence_writer  # 测试注入：(root, stem, kind, payload)->ref
        # Phase 2 Task1：None=Phase 1 行为（revert 单文件回滚）；注入后启用
        # learning 双路召回 + 采纳遥测（coding 站，site="coding"）。
        self.recall = recall
        # L-1：None=调用方未注入；默认目录 phase1_root()/.call_cache 延迟到
        # 首用（且 spec.run_idx 非 None）才建，避免 Task4 直连在 /mnt 留件。
        self._call_cache: "Optional[CallReplay]" = call_cache

    def _replay_cache(self, spec: "RunSpec") -> "Optional[CallReplay]":
        """L-1：仅 run_idx 显式给出时启用（稳定 key coding_{t}_r{idx}_round{n}
        的前提）；默认缓存目录延迟创建。run_idx=None 一律不查不写。"""
        if spec.run_idx is None:
            return None
        if self._call_cache is None:
            self._call_cache = CallReplay(phase1_root() / ".call_cache")
        return self._call_cache

    @staticmethod
    def _usage_ints(usage: Any) -> "Optional[tuple[int, int]]":
        """真实网关返回两个非负 int；fake/无计量网关可能给 (None, None)——
        此类结果不入重放缓存（CallReplay.put 不接受非 int）。"""
        if (isinstance(usage, (list, tuple)) and len(usage) == 2
                and all(isinstance(v, int) and not isinstance(v, bool)
                        and v >= 0 for v in usage)):
            return int(usage[0]), int(usage[1])
        return None

    def _root(self, spec: RunSpec) -> Path:
        if self._evidence_root is not None:
            d = self._evidence_root / spec.thread_id
        else:
            d = _default_evidence_root(spec.thread_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _evidence(self, root: Path, stem: str, kind: str, payload: Any) -> str:
        if self._write_evidence is not None:
            return self._write_evidence(root, stem, kind, payload)
        p = root / f"{stem}.{kind}.json"
        # S-6：write→chmod 收口 fsutil 原子写（mkstemp 0600→fsync→replace）。
        atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        return f"phase1/task4_runner/{root.name}/{p.name}"

    @staticmethod
    def _video_artifacts(workdir: Path, spec: "RunSpec") \
            -> "tuple[Optional[str], Optional[int], Optional[str]]":
        """Task7：从 workdir 真实产物回填视频四字段（不存在即 None，不合成）。

        duration_ms 优先取场景 manifest 的 n_frames×frame_dt_s（免解码）；
        manifest 缺失再用 imageio 读 fps 并逐帧计数（venv pin 2.36.1）。
        intranet_url 恒 None：Gate2 先走原生视频 ≤28MB 直传，全失败才由
        feishu video 阶梯自行降级，runner 不编造内网地址。
        """
        if not spec.video_relpath:
            return None, None, None
        video = (workdir / spec.video_relpath)
        if not video.is_file():
            return None, None, None
        duration_ms: "Optional[int]" = None
        if spec.manifest_relpath:
            mf = workdir / spec.manifest_relpath
            if mf.is_file():
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                    n_frames = meta.get("n_frames")
                    frame_dt = meta.get("frame_dt_s")
                    if isinstance(n_frames, int) and n_frames > 0 \
                            and isinstance(frame_dt, (int, float)) and frame_dt > 0:
                        duration_ms = int(round(n_frames * float(frame_dt) * 1000))
                except (OSError, ValueError):
                    duration_ms = None
        if duration_ms is None:
            try:
                import imageio.v3 as iio  # type: ignore
                meta_i = iio.immeta(video)
                fps = float(meta_i.get("fps") or 0.0)
                if fps > 0:
                    duration_ms = int(round(
                        sum(1 for _ in iio.imiter(video)) / fps * 1000))
            except Exception:  # noqa: BLE001 - 解码失败不伪造，交 Gate2 降级
                duration_ms = None
        cover = None
        if spec.cover_relpath and (workdir / spec.cover_relpath).is_file():
            cover = str(workdir / spec.cover_relpath)
        return str(video), duration_ms, cover

    def run(self, spec: RunSpec) -> dict:
        """执行一次 run：失败 → 至多 5 轮 coding 补救 → 重跑。"""
        workdir = Path(spec.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        failure_marker = workdir / FAILURE_JSON
        if failure_marker.is_file():
            # 计划 L505 ③：已结案 run 再进入，计数处硬停，零网络
            raise CodingLimitError(
                f"{spec.thread_id} 已有 {FAILURE_JSON}，禁止重启失败 run")

        root = self._root(spec)
        telemetry = (workdir / spec.telemetry_relpath).resolve(strict=False)
        timeline: list[dict] = []
        coding_calls = 0
        last_result: Optional[_ExecResult] = None

        while True:
            exec_no = len(timeline) + 1
            try:
                result = self._run(list(spec.cmd), workdir,
                                   spec.run_timeout_s, dict(spec.env_extra))
            except subprocess.TimeoutExpired as exc:
                result = _ExecResult(124, exc.stdout or "",
                                     (exc.stderr or "") + f"\nTIMEOUT after "
                                     f"{spec.run_timeout_s}s")
            timeline.append({"exec": exec_no, "rc": result.rc,
                             "stderr_tail": _tb_tail(result)[-500:]})
            last_result = result
            if result.rc == 0 and telemetry.is_file():
                vp, dur_ms, cover = self._video_artifacts(workdir, spec)
                return {"status": "ok", "telemetry_uri": str(telemetry),
                        "coding_round": coding_calls, "executions": exec_no,
                        "timeline": timeline,
                        "video_path": vp, "duration_ms": dur_ms,
                        "cover_png": cover, "intranet_url": None}

            # —— 失败路径：5 次调用用尽即结案，结构上不存在第 6 次调用 ——
            if coding_calls >= MAX_CODING_ROUNDS:
                return self._fail(root, spec, timeline, last_result,
                                  reason=f"{MAX_CODING_ROUNDS} 轮 coding_debug "
                                         f"后仍失败（D10 上限）")

            tail = _tb_tail(result)
            snippets = _collect_snippets(workdir, tail)
            # Phase 2 Task1：recall 注入时 kb 路由 ProductionRecall 同参查询
            # （query_kb(namespace=troubleshooting, top_k=TOP_K)），
            # ret["kb_hits"] 与原 hits 同源同语义；learning 路只查真实学习表。
            learning_hits: list[dict] = []
            if self.recall is not None:
                ret = self.recall.retrieve(
                    "仿真失败 " + tail[-500:], site="coding")
                hits = ret["kb_hits"]
                learning_hits = ret["learning_hits"]
            else:
                hits = self.vector_engine.query_kb(
                    "仿真失败 " + tail[-500:], namespace="troubleshooting",
                    top_k=TOP_K)
            messages = _build_messages(
                gate2_notes=spec.gate2_notes, traceback_tail=tail,
                snippets=snippets, hits=hits, cmd=spec.cmd,
                learning_hits=learning_hits or None)
            # 硬规则1：runner 永不构造 image block（双保险，网关入口亦拒）
            for mm in messages:
                if not isinstance(mm.get("content"), str):
                    raise PatchSafetyError("runner 消息出现非文本块（硬规则1）")

            coding_calls += 1
            stem = f"coding_round{coding_calls}"
            model = self.gateway.model_id(STAGE)
            # L-1 调用级幂等：kill-9 落在实呼返回后、节点返回前，LangGraph
            # 0.2.28 会整节点重放。稳定 key + 请求指纹命中即复用上次 raw，
            # 不发网、不进 billing_context、不写 ledger，req/resp 证据不动；
            # 补丁仍要重放应用（kill 可能落在补丁落盘前）。
            cache = self._replay_cache(spec)
            cache_key = (f"coding_{spec.thread_id}_r{spec.run_idx}"
                         f"_round{coding_calls}")
            # S-8：指纹并入 stage/model，同 prompt 跨阶段/跨模型不得错配重放。
            # Task1：prompt JSON 串（sort_keys=True 沿用既有缓存口径）同时是
            # 采纳遥测 prompt_sha256 的规范串——两 sha 同源可交叉对账。
            prompt_json = json.dumps(messages, ensure_ascii=False,
                                     sort_keys=True)
            fp = request_fingerprint(
                prompt_json,
                None,
                {"max_tokens": MAX_TOKENS, "temperature": TEMPERATURE},
                stage=STAGE, model=model)
            cached = cache.get(cache_key, fp) if cache is not None else None
            if cached is not None:
                raw = cached["raw"]
                # Task1：重放不重复记采纳行——首呼已记，节点重放属同一逻辑
                # 调用，与 0 ledger/0 证据覆盖语义对齐。
            else:
                req_ref = self._evidence(root, stem, "req", {
                    # P2（Task0）：Phase 2 新证据行时间戳统一严格 UTC Z。
                    "ts_iso": utc_now_iso_z(),
                    "stage": STAGE, "model": model,
                    "thread_id": spec.thread_id, "batch_id": spec.batch_id,
                    "coding_round": coding_calls, "gate2_notes_present":
                        bool(spec.gate2_notes and spec.gate2_notes.strip()),
                    "messages": json.loads(redact(json.dumps(
                        messages, ensure_ascii=False))),
                })
                # 锁内适配（计划 L501）：evidence 必须在 billing_context 进入之后
                # 挂接——该上下文管理器每次创建新 BillingContext，提前 set 会被重置。
                with billing_context(task=spec.billing_task, batch_id=spec.batch_id,
                                     thread_id=spec.thread_id):
                    set_current_evidence(req_ref)
                    raw, _usage = self.gateway.call(
                        STAGE, messages, max_tokens=MAX_TOKENS,
                        temperature=TEMPERATURE)
                self._evidence(root, stem, "resp",
                               {"ok": True, "text": redact(raw)})
                # 成功返回后才入缓存（异常路径不 put，按 miss 安全重付）。
                usage_ints = self._usage_ints(_usage)
                if cache is not None and usage_ints is not None:
                    cache.put(cache_key, kind=STAGE, model=model, raw=raw,
                              usage=usage_ints, fingerprint=fp)
                # Task1 采纳遥测（QA-1 M1）：判定点在 call **成功返回后**、
                # resp 证据写完、cache.put 之后。唯一条件=call 成功返回：
                # 发网前失败/异常路径 0 采纳行，replay 分支不记；采纳不依赖
                # 缓存写入（cache 为 None 或 fake 无计量 usage_ints=None 时
                # 仍记——test_repair_fragments 的成功 fake 即此路径）。崩溃
                # 只漏记不重记（安全方向）。prompt 文本逐字未改，
                # prompt_sha256 仍是实际发送串，learning_text 取其在
                # prompt_json 中的逐字出现形（JSON 转义形）。
                if self.recall is not None:
                    for hit in learning_hits:
                        self.recall.record_adoption(
                            thread_id=spec.thread_id, site="coding",
                            learning_id=hit["id"], prompt_text=prompt_json,
                            learning_text=prompt_embedded_text(
                                str(hit.get("text", ""))))
            try:
                applied = _parse_and_apply(workdir, raw)
            except PatchSafetyError as exc:
                # 越界/坏补丁：拒绝且不发起重试（L505 ②），直接结案
                self._evidence(root, stem, "decision",
                               {"applied": [], "rejected": str(exc)})
                return self._fail(
                    root, spec, timeline, last_result,
                    reason=f"补丁安全拒绝（不重试）：{exc}")
            self._evidence(root, stem, "decision",
                           {"applied": applied, "rejected": None})
            timeline[-1]["coding_round"] = coding_calls
            timeline[-1]["applied"] = applied

    def _fail(self, root: Path, spec: RunSpec, timeline: list[dict],
              result: Optional[_ExecResult], *, reason: str) -> dict:
        # L-4：失败结案被重放（kill 落在 FAILURE.json 落盘后、节点返回前）时，
        # 保留首次写入的 ts_iso，时间线以首次失败时刻为准，不滚动成新时间戳。
        # P2（Task0）：首写时间戳改严格 UTC Z；旧件 +0000/+0800 串原样保留。
        ts_iso = utc_now_iso_z()
        existing_p = Path(spec.workdir) / FAILURE_JSON
        if existing_p.is_file():
            try:
                old_ts = json.loads(existing_p.read_text(
                    encoding="utf-8")).get("ts_iso")
                if isinstance(old_ts, str) and old_ts.strip():
                    ts_iso = old_ts
            except (ValueError, OSError):
                pass
        payload = {
            "status": "failed",
            "thread_id": spec.thread_id,
            "reason": reason,
            "coding_round": len([t for t in timeline if "coding_round" in t]),
            "executions": len(timeline),
            "last_rc": result.rc if result else None,
            "last_stderr_tail": _tb_tail(result)[-2000:] if result else "",
            "timeline": timeline,
            "ts_iso": ts_iso,
        }
        p = Path(spec.workdir) / FAILURE_JSON
        # S-6：原子写 0600；L-4 首写 ts 保留语义在上方不变。
        atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        self._evidence(root, "failure", "json", payload)
        return {"status": "failed", "failure_json": str(p),
                "coding_round": payload["coding_round"],
                "executions": len(timeline), "timeline": timeline,
                "reason": reason}


def agent_run_seam(runner: AgentRunner, spec_factory: "Callable[[dict], RunSpec]"):
    """图接缝工厂：agent_run 节点每次按当前 state 构造 RunSpec 后执行。

    Gate2 打回意见由 spec_factory 从 state 的 gate2_rejected 事件提取。
    """
    def seam(state: dict) -> dict:
        spec = spec_factory(state)
        record = runner.run(spec)
        if record["status"] == "ok":
            return {"telemetry_uri": record["telemetry_uri"],
                    "coding_round": record["coding_round"],
                    "executions": record.get("executions"),
                    "timeline": record.get("timeline", []),
                    "status": "ok",
                    "video_path": record.get("video_path"),
                    "duration_ms": record.get("duration_ms"),
                    "cover_png": record.get("cover_png"),
                    "intranet_url": record.get("intranet_url")}
        return {"telemetry_uri": "", "status": "failed",
                "coding_round": record["coding_round"],
                "failure_json": record["failure_json"],
                "reason": record["reason"]}
    return seam
