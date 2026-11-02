"""agentic 内层循环引擎（AC-T4-6/9/12/13）。

本件是产品化 CodeAgent 的唯一装配点：

- 六组工具 + finish/fail 包成 smolagents ``Tool``，每个 forward 固定走
  预算计数 → 工具层（jail→schema→policy→执行）→ 观察规范化 → 轨迹前缀
  推进 → trace.jsonl 一行（第二重 forward 闸与取证都在这一层）；
- 模型是 :class:`~orchestrator.agentic.model.GatewayModel`，执行器是
  :class:`~orchestrator.agentic.executor.HardenedLocalPythonExecutor`
  （构造时直接注入，CodeAgent 的 create_python_executor 不参与）；
- ``code_block_tags="markdown"``：Stage0 探针实测模型 0/2 吐字面
  ``<code>`` 标签、2/2 吐 ```python 围栏，产品显式约束输出形态；
- Condenser：旧轮只留哈希+摘要，最近两轮全文；迷路计数（截断丢字符数）
  以 ``__condense__`` 行入 trace（AC-T4-12）；
- kill-9 续写：smolagents 1.26 无官方运行期 checkpoint，续写原语是
  :func:`feed_trajectory_from_trace`——从全量存储重建历史观察、重放
  note_step 前缀链，**绝不重跑工具**；同进程/可读 trace 时下一指纹与
  原进程一致故缓存命中 0 seq，不可读则诚实按重出生计费（由接线方标注）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from smolagents.agents import AgentGenerationError

# Task 5b：协议反馈包装子类替换库存 CodeAgent（唯一换装点；冻结规格
# scripts/phase25/walls/task5b_spec.md §3.1/§3.3，攢批115 af2df49）。
from .agent_code import ProtocolHardenedCodeAgent as CodeAgent
from smolagents.memory import ActionStep
from smolagents.monitoring import LogLevel
from smolagents.tools import Tool

from .caps import (
    KIND_BIRTH_ABSENT,
    KIND_ENGINE_ERROR,
    KIND_FAIL,
    KIND_FINISH,
    KIND_ROUND_CAP,
    KIND_ROUND_TAIL,
    KIND_STEP_CAP,
    KIND_TOKEN_CAP,
    BudgetTracker,
    CapExceeded,
)
from .config import ALL_OFF, AgenticConfig
from .executor import HardenedLocalPythonExecutor, TerminateSignal
from .forensics import (
    ANSWER_TOOL,
    CACHE_LOGICAL_PREFIX,
    CACHE_LOGICAL_PREFIX_LEGACY,
    CONDENSE_TOOL,
    MODEL_ROUND_TOOL,
    TraceStore,
    cache_epoch,
    canonical_json,
    iter_trajectory,
    normalize_observation,
    sha256_bytes,
    sha256_text,
)
from .model import SupplierModelError, build_gateway_model
from .policy import (
    GRANT_SOURCE_CONTROL,
    GrantTable,
    PolicyDecision,
    ToolContext,
)
from .tools import ToolResult
from .tools.read import grep as t_grep
from .tools.read import list_dir as t_list_dir
from .tools.read import read_file as t_read_file
from .tools.probe import run_probe as t_run_probe
from .tools.recall import recall as t_recall
from .tools.run import run_cmd as t_run_cmd
from .tools.run_expr import MAX_EXPR_CHARS, run_expr as t_run_expr
from .tools.scratchpad import (
    SCRATCHPAD_MAX_CHARS,
    SCRATCHPAD_REL,
    update_scratchpad as t_update_scratchpad,
)
from .tools.terminate import fail as t_fail
from .tools.terminate import finish as t_finish
from .tools.write import apply_patch as t_apply_patch
from .tools.write import write_file as t_write_file

KIND_SUPPLIER_ERROR = "supplier_error"
KIND_MAX_STEPS = "max_steps_error"

# ---------------------------------------------------------------------------
# Phase 2.6 Task3（F1）：出生判定与验证-修复尾
# ---------------------------------------------------------------------------

# 入口脚本的**唯一真值**（本文件定名；其他段落/测试只引用不重复字面量）。
ENTRYPOINT = ".entrypoint.py"
ENTRYPOINT_MIN_BYTES = 200
TAIL_DIRECTIVE = ("【验证-修复尾】入口脚本已出生，剩余轮次只用于运行测试、"
                  "阅读报错、应用补丁。\n不要再新建文件，不要重写无关模块；"
                  "每轮必须以一次验证命令开头。")


def _entrypoint_ready(workdir: Path, name: str = ENTRYPOINT) -> bool:
    """三条件全真才算「出生」：存在 + 体积达标 + 编译通过。

    编译语义与 py_compile 同源（``py_compile.compile`` 内部就是
    ``compile(source_bytes, cfile, dfile)``），但不落 ``.pyc``：

    锁内适配（Phase 2.6 Task3，计划 §T3.4-I 示例行）：计划写的是
    ``-m py_compile``，那会在 workdir 里写 ``__pycache__``（每次出生检测
    都污染工作目录）；改 ``cfile='/dev/null'`` 又被 py_compile 直接拒
    （``FileExistsError: /dev/null is a non-regular file``，实测）。故只做
    ``compile()`` 解析/编译这一步、不写字节码——同样的 SyntaxError 判定，
    零副作用。``-I`` 隔离模式防工作目录里的影子模块劫持（与 run_expr 同向），
    解释器与执行镜像同版本（容器内外版本差异会把合法语法判成 SyntaxError，
    这是 §T3.6「失败处理」第一条）。
    """
    p = Path(workdir) / name
    try:
        if not p.is_file() or p.stat().st_size < ENTRYPOINT_MIN_BYTES:
            return False
    except OSError:
        return False
    code = ("import sys;"
            "compile(open(sys.argv[1], 'rb').read(), sys.argv[1], 'exec')")
    try:
        r = subprocess.run([sys.executable, "-I", "-c", code, str(p)],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _read_scratchpad(workdir: Path, *, enabled: bool
                     ) -> "tuple[str, bool]":
    """读一次 scratchpad（返回 (文本, 是否截断)）；未启用恒 ("", False)。

    截断发生在读取侧：``update_scratchpad`` 已拒 >4000 字符的写入，这里
    只防外部直接编辑文件把 scratchpad 变成绕过 token_cap 的后门（D3-8）。
    """
    if not enabled:
        return "", False
    p = Path(workdir) / SCRATCHPAD_REL
    try:
        data = p.read_bytes()
    except OSError:
        # 不存在 / 不是普通文件 / 读不了：都当「没有记忆」，不拦 run。
        return "", False
    text = data.decode("utf-8", errors="replace")
    if len(text) > SCRATCHPAD_MAX_CHARS:
        return text[:SCRATCHPAD_MAX_CHARS], True
    return text, False


def _tool_docs(cfg: "AgenticConfig | None") -> str:
    """开关开启时才追加的工具说明（全关时 prompt 与旧版逐字节相同）。"""
    if cfg is None or not cfg.any_enabled:
        return ""
    docs = ""
    if cfg.run_expr:
        docs += (f"- run_expr(src)：在隔离容器里执行一个小程序（≤{MAX_EXPR_CHARS}"
                 " 字符；只允许 import 白名单模块 + 表达式语句；"
                 "禁止赋值/定义/控制流/dunder）。\n")
    if cfg.scratchpad:
        docs += (f"- update_scratchpad(content)：整体覆盖写跨轮记忆 "
                 f"{SCRATCHPAD_REL}（≤{SCRATCHPAD_MAX_CHARS} 字符）；下一轮"
                 "生效，压缩旧轮不会丢它。\n")
    return ("\n本 run 追加工具：\n" + docs) if docs else ""

# C3：loop 唯一信任的真终局信号 kind 白名单——来源只能是硬化执行器
# _terminal_sig（TerminateSignal/CapExceeded 两个 BaseException，模型 blob
# 的 except Exception 接不住）。output dict 里的 __agentic_terminate__ 标记
# 可经 final_answer(answer={...}) 逐字伪造，不作为分类依据。
# C4：KIND_ENGINE_ERROR 入白名单——executor 在不可信 blob 边界把
# SystemExit/KeyboardInterrupt/GeneratorExit/裸 BaseException 家族
# fail-closed 合成 TerminateSignal（见 executor.py __call__ 第三 clause）。
TERMINAL_SIG_KINDS = frozenset({
    KIND_FINISH, KIND_FAIL, KIND_STEP_CAP, KIND_TOKEN_CAP, KIND_ROUND_CAP,
    KIND_ROUND_TAIL, KIND_BIRTH_ABSENT, KIND_ENGINE_ERROR})

# AC-T4-12：上下文工程常量的集中配置点（另见 forensics.OBS_*_CHARS）。
KEEP_FULL_ROUNDS = 2
# smolagents 单轮代码内打印上限（与上游默认同量级，超长走 trace 全量）。
MAX_PRINT_OUTPUTS = 20_000

_SYSTEM_CONTRACT = """\
你在 SimPraxis agentic 内环中工作。你只能通过被授予的 Python 工具函数行动。

工具契约：
- read_file(path, max_lines=2000)：读 workdir 内文本文件；路径必须是相对路径。
- list_dir(path=".")：列 workdir 内目录。
- grep(pattern, path=".", max_matches=200)：workdir 内正则搜索，拒二进制。
- write_file(path, content)：原子写（.py 必须语法合法）；仅 Gate1 可写前缀。
- apply_patch(patch)：补丁通道（replace.v1 围栏 / JSON structured/unified_diff）；
  异名锚点拒绝；禁止 Aider 式模糊匹配。
- run_cmd(argv, timeout_s=300, env=None)：argv 必须是字符串列表，永不经过
  shell；只有 Gate1 argv 白名单前缀可执行；默认在禁网兄弟容器内运行。
- run_probe(name, timeout_s=600)：只能执行白名单平台探针；网络探针默认拒绝。
- recall(query, k=5)：只读经验召回。
- finish(answer, reason="")：声称完成。**finish 不等于通过**——后续评估与
  Gate2 独立判定，禁止用 finish 掩盖未完成项。
- fail(reason)：确认不可恢复时诚实终局（不要用异常或空转耗尽轮帽）。

工作纪律：
1. 先读后改：任何 write_file/apply_patch 前必须先 read_file 看到当前内容。
2. 最小命令：一次 run_cmd 只做一件事；禁止 import（os/subprocess/socket 等
   全部零授权），需要能力只能调工具；禁止 dunder、open/eval/exec。
3. 每轮自检：本轮观察是否证明了目标？没有就调整策略，不要重复同一失败动作。
4. 所有路径都是 workdir 相对路径；``..`` 逃逸、绝对路径、软链外跳一律拒绝。
5. 被 POLICY DENY 拒绝时零副作用已保证：读懂拒绝原因，换合法做法或 fail。

输出格式（每轮必须严格遵守；框架要求 Thought 与代码块成对出现，二者缺一
即失败）：
- 每轮输出恰好两部分，顺序固定：第一行 `Thought: ` 一句话动作，下一行
  立即开始一个闭合的 ```python 围栏，块内只放调用工具的 Python 代码。
- 只有 Thought 没有围栏＝空转：不执行任何工具，但**照样计 1 次付费调用、
  占 1 轮轮帽**。裸 ```、未闭合围栏、空块、多围栏均按空转换账。
- 围栏之外禁止任何其他文本；每轮至多一个围栏。
- 正例：
  Thought: 先看工作目录里有什么。
  ```python
  print(list_dir("."))
  ```
- 反例（错误，0 个工具调用且计费轮照占）：
  Thought: I will inspect the directory.
  （这行之后必须紧跟闭合 ```python 围栏，不能什么都没有）
- 终局轮保持同一形状，在围栏内调用 finish(...) 或 fail(...)。
"""


def system_contract(*, extra: str = "") -> str:
    return _SYSTEM_CONTRACT + (f"\n本 run 附加约束：\n{extra}\n" if extra else "")


# ---------------------------------------------------------------------------
# smolagents Tool 绑定（forward 闸 + trace + 预算 + 前缀链）
# ---------------------------------------------------------------------------

_TOOL_FNS: dict[str, Callable[..., ToolResult]] = {
    "read_file": t_read_file,
    "list_dir": t_list_dir,
    "grep": t_grep,
    "write_file": t_write_file,
    "apply_patch": t_apply_patch,
    "run_cmd": t_run_cmd,
    "run_probe": t_run_probe,
    "recall": t_recall,
    "finish": t_finish,
    "fail": t_fail,
    # Phase 2.6 Task3：F2/F4（按 cfg 动态出现在授予面，见 granted_tool_names）。
    "run_expr": t_run_expr,
    "update_scratchpad": t_update_scratchpad,
}

_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "read_file": {
        "description": "读取 workdir 内文本文件（相对路径；拒二进制/越界/超大）。",
        "inputs": {
            "path": {"type": "string", "description": "workdir 相对路径"},
            "max_lines": {"type": "integer", "nullable": True,
                          "description": "最多返回行数，默认 2000"},
        },
    },
    "list_dir": {
        "description": "列出 workdir 内目录（相对路径）。",
        "inputs": {
            "path": {"type": "string", "nullable": True,
                     "description": "workdir 相对路径，默认 '.'"},
        },
    },
    "grep": {
        "description": "workdir 内正则搜索（跳过二进制，默认上限 200 匹配）。",
        "inputs": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "nullable": True,
                     "description": "搜索根的相对路径，默认 '.'"},
            "max_matches": {"type": "integer", "nullable": True,
                            "description": "匹配上限，默认 200"},
        },
    },
    "write_file": {
        "description": "原子写文件（相对路径；.py 必须 AST 合法；仅可写前缀）。",
        "inputs": {
            "path": {"type": "string", "description": "workdir 相对路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
    },
    "apply_patch": {
        "description": "以 replace.v1 围栏或 JSON 补丁通道应用补丁（异名锚点拒绝）。",
        "inputs": {
            "patch": {"type": "string", "description": "补丁全文（围栏或 JSON）"},
        },
    },
    "run_cmd": {
        "description": ("在禁网兄弟容器执行 argv 列表（不经 shell；仅白名单前缀；"
                        "stdout/stderr 全量取证、头尾截断回显）。"),
        "inputs": {
            "argv": {"type": "array", "items": {"type": "string"},
                     "description": "命令 argv 字符串列表，如 ['python','-V']"},
            "timeout_s": {"type": "integer", "nullable": True,
                          "description": "超时秒，默认 300"},
            "env": {"type": "object", "nullable": True,
                    "description": "白名单环境变量键值对（可省略）"},
        },
    },
    "run_probe": {
        "description": "执行白名单平台探针（网络探针须逐项 Gate1 授权）。",
        "inputs": {
            "name": {"type": "string", "description": "探针名（见白名单）"},
            "timeout_s": {"type": "integer", "nullable": True,
                          "description": "超时秒，默认 600"},
        },
    },
    "recall": {
        "description": "只读经验召回，返回 id/source/relpath 与块文本。",
        "inputs": {
            "query": {"type": "string", "description": "检索查询"},
            "k": {"type": "integer", "nullable": True,
                  "description": "返回条数，默认 5"},
        },
    },
    "finish": {
        "description": "声称完成（不等于通过；评估与 Gate2 独立判定）。",
        "inputs": {
            "answer": {"type": "string", "description": "交付说明/产物路径"},
            "reason": {"type": "string", "nullable": True,
                       "description": "可选说明"},
        },
    },
    "fail": {
        "description": "确认不可恢复时诚实终局。",
        "inputs": {
            "reason": {"type": "string", "description": "不可恢复原因"},
        },
    },
    "run_expr": {
        "description": ("在隔离容器里执行一个小程序（只允许 import 白名单模块 + "
                        "若干表达式语句）并返回 stdout；禁止赋值/定义/控制流/"
                        "任何 dunder。"),
        "inputs": {
            "src": {"type": "string",
                    "description": f"程序源码（≤{MAX_EXPR_CHARS} 字符）"},
        },
    },
    "update_scratchpad": {
        "description": (f"整体覆盖写跨轮记忆 {SCRATCHPAD_REL}"
                        f"（≤{SCRATCHPAD_MAX_CHARS} 字符，下一轮生效）。"),
        "inputs": {
            "content": {"type": "string", "description": "完整 scratchpad 内容"},
        },
    },
}


class RoundState:
    """C2：引擎私有的每-run 轮态（最近实呼 seq/轮号/raw ref）。

    旧实现把这三个字段挂在可变 ``ToolContext`` 上，模型代码经
    ``read_file.ctx.current_seq`` 穿透即可读改写。现在 ctx 是 frozen 对象、
    不携轮态；本 holder 只存在于 recorder 闭包与工具 invoker 闭包的细胞里，
    模型代码 blob 无任何名字可达（slots 固定三字段）。
    """

    __slots__ = ("current_round", "current_seq", "current_raw_ref")

    def __init__(self) -> None:
        self.current_round = 0
        # M5：最近一次真实网关调用的 ledger 整数 seq（缓存命中轮本身
        # seq=null，但其工具行仍挂最近实呼 seq）；从未实呼为 None。
        self.current_seq: "int | None" = None
        self.current_raw_ref: "str | None" = None


class BoundTool(Tool):
    """**仅 schema 用**的 smolagents Tool（C2：不再持有任何活句柄）。

    C2（2026-10-30 CRITICAL 返修）：旧实现把 ``fn/ctx/model/tracer/
    step_counter`` 挂为实例属性，而 smolagents ``evaluate_name`` 每次名字
    求值都用 ``safer_func`` 包一层，``@wraps`` 复制 ``__dict__``——读穿透
    到共享活对象，模型代码零工具即可 ``read_file.fn(FakeCtx())`` 绕 jail、
    ``finish.model.gateway.call(...)`` 逃付费、``finish.ctx.jail=Fake()``。
    现在：

    - 模型 blob 在硬化执行器内拿到的是 :func:`build_tool_invokers` 造的
      **无属性裸闭包**（``__dict__`` 为空，活句柄全在闭包细胞里），由
      :class:`~orchestrator.agentic.executor.HardenedLocalPythonExecutor`
      .send_tools 按名替换，从不拿到本类实例；
    - 本类实例只服务 CodeAgent 的工具清单/参数 schema 生成（agent.tools
      需要 Tool 实例），forward 不经过产品路径，保留显式签名仅为满足
      smolagents 元类的抽象方法与参数名校验。

    具体工具仍以匹配 ``inputs`` 键的显式签名子类化；共享调用逻辑在
    :func:`build_tool_invokers` 的裸闭包里。
    """

    output_type = "string"

    def __init__(self) -> None:
        spec = _TOOL_SPECS[self.name]
        self.description = spec["description"]
        self.inputs = spec["inputs"]
        super().__init__()

    def forward(self, *args: Any, **kwargs: Any) -> str:
        # 产品路径永不调本方法（执行器按名换成裸闭包）；误调即编程错误。
        raise RuntimeError(
            f"BoundTool({self.name}) 仅用于工具 schema；模型调用必须走"
            "硬化执行器内的无属性工具闭包（C2）")


class _ReadFileTool(BoundTool):
    name = "read_file"

    def forward(self, path: str, max_lines: int = 2000) -> str:
        return super().forward()


class _ListDirTool(BoundTool):
    name = "list_dir"

    def forward(self, path: str = ".") -> str:
        return super().forward()


class _GrepTool(BoundTool):
    name = "grep"

    def forward(self, pattern: str, path: str = ".",
                max_matches: int = 200) -> str:
        return super().forward()


class _WriteFileTool(BoundTool):
    name = "write_file"

    def forward(self, path: str, content: str) -> str:
        return super().forward()


class _ApplyPatchTool(BoundTool):
    name = "apply_patch"

    def forward(self, patch: str) -> str:
        return super().forward()


class _RunCmdTool(BoundTool):
    name = "run_cmd"

    def forward(self, argv: list, timeout_s: int = 300,
                env: "dict | None" = None) -> str:
        return super().forward()


class _RunProbeTool(BoundTool):
    name = "run_probe"

    def forward(self, name: str, timeout_s: int = 600) -> str:
        return super().forward()


class _RecallTool(BoundTool):
    name = "recall"

    def forward(self, query: str, k: int = 5) -> str:
        return super().forward()


class _FinishTool(BoundTool):
    name = "finish"

    def forward(self, answer: str, reason: str = "") -> str:
        return super().forward()


class _FailTool(BoundTool):
    name = "fail"

    def forward(self, reason: str) -> str:
        return super().forward()


class _RunExprTool(BoundTool):
    name = "run_expr"

    def forward(self, src: str) -> str:
        return super().forward()


class _UpdateScratchpadTool(BoundTool):
    name = "update_scratchpad"

    def forward(self, content: str) -> str:
        return super().forward()


class RoundMeta:
    """每-run 的轮面元数据（F1 追授 + F4 scratchpad + 缓存纪元）。

    与 :class:`RoundState` 同族：只活在 loop/模型工厂的闭包细胞里，模型
    代码无名字可达。``any_enabled`` 为假时 trace args 与旧版逐字节相同
    （AC-T3-5）；为真时才追加 phase/scratchpad/tail 元数据。
    """

    __slots__ = ("any_enabled", "cache_version", "cache_logical_prefix",
                 "phase", "tail_granted", "effective_round_cap",
                 "scratchpad_enabled", "scratchpad_text",
                 "scratchpad_sha256", "scratchpad_chars",
                 "scratchpad_truncated", "directive")

    def __init__(self, cfg: AgenticConfig, *, cache_version: str,
                 cache_logical_prefix: str) -> None:
        self.any_enabled = cfg.any_enabled
        self.cache_version = cache_version
        self.cache_logical_prefix = cache_logical_prefix
        self.phase = "explore"
        self.tail_granted = False
        self.effective_round_cap = 0
        self.scratchpad_enabled = cfg.scratchpad
        self.scratchpad_text = ""
        self.scratchpad_sha256 = ""
        self.scratchpad_chars = 0
        self.scratchpad_truncated = False
        self.directive = ""


def _make_invoker(name: str, *, ctx: ToolContext, model: Any,
                  tracer: "TraceStore | None", budget: "BudgetTracker | None",
                  round_state: RoundState, step_counter: list[int],
                  on_step: "Callable[[], None] | None" = None
                  ) -> Callable[..., str]:
    """造一个工具名的**无属性裸闭包**（C2 核心）。

    闭包 ``__dict__`` 为空、不经 functools.wraps：smolagents safer_func
    再包一层时 @wraps 复制到的只是空字典与字符串槽（__name__/__doc__），
    ``read_file.fn/.ctx/.model/.tracer/.step_counter`` 全部 AttributeError；
    活句柄只活在本工厂的闭包细胞里，不是名字可达的属性。调用逻辑与旧
    BoundTool._invoke 逐字一致：预算帽 → 工具层（jail→schema→policy→执行）
    → 步计数 → 规范化观察/前缀链 → trace 行。
    """
    fn = _TOOL_FNS[name]
    control = name in ("finish", "fail")
    arg_keys = tuple(_TOOL_SPECS[name]["inputs"])

    def invoker(*args: Any, **kwargs: Any) -> str:
        # 模型合同是关键字调用；位置形态按 schema 声明序归一（工具 fn 只读
        # kwargs，默认值由 schema.validate 按字段默认填）。
        if args:
            for key, value in zip(arg_keys, args):
                kwargs[key] = value
        t0 = time.monotonic()
        # 控制原语不占工具步帽（到帽时仍须能 finish/fail 收尾）。
        if not control and budget is not None:
            budget.check_tool_step()
        # TerminateSignal/CapExceeded 是 BaseException 终局信号，原样穿过
        # 闭包进硬化执行器映射，不允许在此吞掉。
        result = fn(ctx, **kwargs)
        latency_ms = int((time.monotonic() - t0) * 1000)

        step = step_counter[0] + 1
        step_counter[0] = step
        if not control and budget is not None:
            budget.note_tool_step()

        # 规范化观察（剥时间戳/临时路径/pid 等）入轨迹前缀；全量原文入存储。
        safe = _safe_args(kwargs)
        norm = normalize_observation(result.full, workdir=ctx.workdir)
        norm_sha = sha256_text(norm)
        model.note_step(
            step=step, tool=name, args=safe,
            normalized_obs_sha256=norm_sha)

        if tracer is not None:
            tracer.record(
                seq=round_state.current_seq,
                round_idx=round_state.current_round, step=step,
                tool=name, args=safe, observation=result.full,
                policy=result.decision.as_trace(), latency_ms=latency_ms,
                raw_response_ref=round_state.current_raw_ref)
        if on_step is not None:
            # F1：每轮工具执行结束后做一次出生检测（追授后自动空转成 no-op）。
            on_step()
        return result.output

    # 字符串槽（不进函数 __dict__）：safer_func 包装后错误/trace 仍可读名。
    invoker.__name__ = name
    invoker.__qualname__ = name
    invoker.__doc__ = _TOOL_SPECS[name]["description"]
    return invoker


def granted_tool_names(ctx: ToolContext,
                       cfg: "AgenticConfig | None" = None) -> tuple[str, ...]:
    """本次授予的工具名（按授予表 + 开关面 + finish/fail 恒在）。

    Phase 2.6 Task3：``run_expr`` 只在 ``cfg.run_expr``、``update_scratchpad``
    只在 ``cfg.scratchpad`` 时出现——cfg 为 None/全关时工具面与旧版逐项
    相同（AC-T3-5）。
    """
    enabled: set[str] = set()
    if cfg is not None and cfg.run_expr:
        enabled.add("run_expr")
    if cfg is not None and cfg.scratchpad:
        enabled.add("update_scratchpad")
    names = tuple(n for n in ("read_file", "list_dir", "grep", "write_file",
                              "apply_patch", "run_cmd", "run_probe", "recall")
                  if n in ctx.grants.tools)
    extra = tuple(n for n in ("run_expr", "update_scratchpad")
                  if n in enabled and n in ctx.grants.tools)
    return names + extra + ("finish", "fail")


def build_tool_invokers(*, ctx: ToolContext, model: Any,
                        tracer: "TraceStore | None",
                        budget: "BudgetTracker | None",
                        round_state: RoundState,
                        step_counter: list[int],
                        cfg: "AgenticConfig | None" = None,
                        on_step: "Callable[[], None] | None" = None
                        ) -> dict[str, Callable[..., str]]:
    """按授予表装配「工具名 → 无属性裸闭包」（执行器侧实际可调用面）。"""
    return {
        name: _make_invoker(
            name, ctx=ctx, model=model, tracer=tracer, budget=budget,
            round_state=round_state, step_counter=step_counter,
            on_step=on_step)
        for name in granted_tool_names(ctx, cfg)
    }


_BOUND_CLASSES: dict[str, type[BoundTool]] = {
    "read_file": _ReadFileTool,
    "list_dir": _ListDirTool,
    "grep": _GrepTool,
    "write_file": _WriteFileTool,
    "apply_patch": _ApplyPatchTool,
    "run_cmd": _RunCmdTool,
    "run_probe": _RunProbeTool,
    "recall": _RecallTool,
    "finish": _FinishTool,
    "fail": _FailTool,
    "run_expr": _RunExprTool,
    "update_scratchpad": _UpdateScratchpadTool,
}


def _safe_args(kwargs: Any) -> dict[str, Any]:
    """工具行 args：dict 化 + 长度硬切（指纹另走 canonical_json 同形）。"""
    if not isinstance(kwargs, dict):
        return {"_raw": str(kwargs)[:1000]}
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        text = v if isinstance(v, str) else canonical_json(v)
        out[k] = text if not isinstance(text, str) or len(text) <= 20_000 \
            else text[:20_000] + "...[args truncated]"
    return out


def build_bound_tools(ctx: ToolContext,
                      cfg: "AgenticConfig | None" = None) -> list[BoundTool]:
    """按授予表装配**schema 工具**（六组仅授表内者出现；finish/fail 恒在）。

    C2：这些 Tool 实例只给 CodeAgent 生成工具清单/参数 schema；模型 blob
    实际调用的是 build_tool_invokers 的无属性裸闭包（执行器 send_tools 按
    名替换）。
    """
    tools: list[BoundTool] = []
    for name in granted_tool_names(ctx, cfg):
        tools.append(_BOUND_CLASSES[name]())
    return tools


# ---------------------------------------------------------------------------
# Condenser（旧轮只留哈希摘要；迷路计数入 trace）
# ---------------------------------------------------------------------------

class Condenser:
    """确定性上下文压缩：仅保留最近 ``keep_full`` 个动作轮全文。

    压缩动作本身由 step_callback 在每轮结束后驱动，被压缩的旧轮替换为
    「工具名 + args_hash + 观察 sha256」摘要；累计丢失字符数（lostness）
    以 __condense__ 控制行进 trace。同轨迹必同压缩（确定性），故指纹可重放。
    """

    def __init__(self, tracer: "TraceStore | None", *,
                 keep_full: int = KEEP_FULL_ROUNDS):
        self.tracer = tracer
        self.keep_full = keep_full
        self.lost_chars = 0
        self.condensed_rounds = 0

    def __call__(self, memory_step: Any, agent: CodeAgent) -> None:
        steps = [s for s in agent.memory.steps if type(s).__name__ == "ActionStep"]
        if len(steps) <= self.keep_full:
            return
        for s in steps[:-self.keep_full]:
            if getattr(s, "_agentic_condensed", False):
                continue
            lost = 0
            calls = []
            if getattr(s, "tool_calls", None):
                for tc in s.tool_calls:
                    args = getattr(tc, "arguments", None)
                    h = sha256_text(canonical_json(args))
                    lost += len(str(args))
                    tc.arguments = {"_condensed": True, "args_hash": h}
                    calls.append(getattr(tc, "name", "?"))
            obs_text = ""
            if getattr(s, "observations", None):
                obs_text = str(s.observations)
                lost += len(obs_text)
                hashes = []
                for line in (self.tracer.load_lines() if self.tracer else []):
                    if line.get("round") == getattr(s, "step_number", -1) \
                            and not str(line.get("tool", "")).startswith("__"):
                        hashes.append(line["obs_sha256"][:12])
                s.observations = (
                    f"[condensed] 旧轮工具 {calls}，观察哈希 {hashes}；"
                    "全文在 trace 全量存储，需要时用 read_file/grep 回查。")
            if getattr(s, "model_output", None):
                lost += len(s.model_output)
                s.model_output = "[condensed] 旧轮代码输出已压缩。"
            s._agentic_condensed = True
            self.condensed_rounds += 1
            self.lost_chars += lost
            if self.tracer is not None:
                self.tracer.record(
                    # M5：condense 是引擎内部事件、无模型调用，seq=null。
                    seq=None, round_idx=int(getattr(s, "step_number", 0)),
                    step=0, tool=CONDENSE_TOOL,
                    args={"condensed_round": getattr(s, "step_number", 0),
                          "lost_chars_this_round": lost,
                          "lost_chars_cumulative": self.lost_chars},
                    observation=f"round {getattr(s, 'step_number', '?')} condensed",
                    policy={"decision": "allow", "reason": "",
                            "grant_source": GRANT_SOURCE_CONTROL},
                    latency_ms=0)


# ---------------------------------------------------------------------------
# 结局
# ---------------------------------------------------------------------------

@dataclass
class AgenticOutcome:
    kind: str
    value: str = ""
    reason: str = ""
    smolagents_state: str = ""
    rounds_used: int = 0
    steps_used: int = 0
    tokens_used: int = 0
    cache_hits: int = 0
    empty_responses: int = 0
    supplier_errors: int = 0
    trace_path: "Path | None" = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_failure_artifact(self) -> bool:
        return self.kind in (
            KIND_FAIL, KIND_STEP_CAP, KIND_TOKEN_CAP, KIND_ROUND_CAP,
            KIND_ROUND_TAIL, KIND_BIRTH_ABSENT,
            KIND_SUPPLIER_ERROR, KIND_ENGINE_ERROR, KIND_MAX_STEPS)


# ---------------------------------------------------------------------------
# 引擎装配与运行
# ---------------------------------------------------------------------------

def _make_recorder(tracer: TraceStore, round_state: RoundState,
                   meta: "RoundMeta | None" = None) -> Callable[..., str]:
    def record_round(round_idx: int, raw_text: str,
                     usage: "tuple[int,int] | None", cache_hit: bool,
                     latency_ms: int, disk_key: "str | None",
                     stopped_by_local: bool = False,
                     stop_sequence_hit: "str | None" = None,
                     seq: "int | None" = None) -> str:
        # raw_text 永远是网关/缓存原文（本地 stop 截断只改 ChatMessage.content，
        # 不改 raw_response_ref 落盘内容——截断证据另以两字段入 args）。
        ref = tracer.store_raw_response(raw_text) if raw_text else ""
        round_state.current_round = round_idx
        # M5：先刷新「最近实呼 seq」，再写模型轮行；其后同轮工具行继承此值。
        # 缓存命中轮 model.py 显式传 None——current_seq 保持上一实呼轮的值，
        # 模型轮行自身记 null，工具行仍挂最近实呼 seq。
        if seq is not None:
            round_state.current_seq = seq
        round_state.current_raw_ref = ref
        # 16 字段契约（NF-6）禁止加列：版本串与本地截断证据放 args 内。
        # cache_namespace 逐字保留逻辑版本串（盘键无冒号，见 forensics）。
        round_args: dict[str, Any] = {
            "cache_hit": cache_hit, "empty_response": not raw_text,
            "disk_key": disk_key,
            "cache_namespace": (meta.cache_logical_prefix if meta is not None
                                else CACHE_LOGICAL_PREFIX),
            "stopped_by_local": stopped_by_local,
            "stop_sequence_hit": stop_sequence_hit,
        }
        if meta is not None and meta.any_enabled:
            # Task3 元数据只在开关开启时出现——全关时 args 逐字节同旧版。
            round_args.update({
                "phase": meta.phase,
                "tail_granted": meta.tail_granted,
                "effective_round_cap": meta.effective_round_cap,
                "scratchpad_chars": meta.scratchpad_chars,
                "scratchpad_sha256": meta.scratchpad_sha256,
                "scratchpad_truncated": meta.scratchpad_truncated,
            })
            if meta.directive:
                round_args["tail_directive"] = meta.directive
        tracer.record(
            seq=seq, round_idx=round_idx, step=0,
            tool=MODEL_ROUND_TOOL,
            args=round_args,
            observation=raw_text,
            policy={"decision": "allow", "reason": "",
                    "grant_source": GRANT_SOURCE_CONTROL},
            latency_ms=latency_ms, raw_response_ref=ref or None,
            usage=list(usage) if usage is not None else None)
        return ref
    return record_round


def build_agent(*, ctx: ToolContext, model: Any,
                tracer: "TraceStore | None",
                budget: "BudgetTracker | None",
                round_state: RoundState,
                instructions: str, max_steps: int,
                step_counter: "list[int] | None" = None,
                step_callbacks: "list[Callable] | None" = None,
                cfg: "AgenticConfig | None" = None,
                on_step: "Callable[[], None] | None" = None
                ) -> tuple[CodeAgent, list[BoundTool], list[int]]:
    """构造产品化 CodeAgent（注入硬化执行器；不触发任何模型调用）。

    C2：schema 工具（富属性 Tool 实例）只进 CodeAgent.tools 供清单生成；
    执行器侧装的是无属性裸闭包 invokers，CodeAgent 每步 send_tools 传入的
    BoundTool 被 HardenedLocalPythonExecutor.send_tools 按名替换。
    """
    step_counter = step_counter if step_counter is not None else [0]
    tools = build_bound_tools(ctx, cfg)
    invokers = build_tool_invokers(
        ctx=ctx, model=model, tracer=tracer, budget=budget,
        round_state=round_state, step_counter=step_counter,
        cfg=cfg, on_step=on_step)
    executor = HardenedLocalPythonExecutor(
        max_print_outputs_length=MAX_PRINT_OUTPUTS, timeout_seconds=30,
        tool_invokers=invokers)
    agent = CodeAgent(
        tools=tools, model=model,
        instructions=instructions,
        additional_authorized_imports=[],
        executor=executor,
        max_steps=max_steps,
        code_block_tags="markdown",
        verbosity_level=LogLevel.OFF,
        step_callbacks=step_callbacks or [],
    )
    return agent, tools, step_counter


def run_agentic_inner_loop(*, workdir: "str | Path", task: str,
                           grants: GrantTable, gateway: Any,
                           budget: BudgetTracker, tracer: TraceStore,
                           thread_id: str,
                           run_backend: Any = None,
                           probe_registry: Any = None,
                           recall_port: Any = None,
                           call_cache: Any = None,
                           nonprod: bool = False,
                           system_extra: str = "",
                           max_steps: "int | None" = None,
                           seq_provider: "Callable[[], int | None] | None" = None,
                           agentic_config: "AgenticConfig | None" = None
                           ) -> AgenticOutcome:
    """跑一次 agentic 内环（本 Task 全部测试用 fake gateway/backend，0 付费）。

    Phase 2.6 Task3：``agentic_config`` 缺省/全关时全面等价 Phase 2.5
    （工具面、prompt、缓存纪元 v2、trace args 逐字节；AC-T3-5）。
    """
    workdir = Path(workdir)
    cfg = agentic_config if agentic_config is not None else ALL_OFF
    cache_version, cache_prefix, _disk_prefix = cache_epoch(cfg.any_enabled)
    meta = RoundMeta(cfg, cache_version=cache_version,
                     cache_logical_prefix=cache_prefix)
    # C2：ctx frozen、不携预算/轮态；预算只在闭包细胞，轮态在私有 holder。
    ctx = ToolContext(
        workdir=workdir, grants=grants, thread_id=thread_id, tracer=tracer,
        run_backend=run_backend,
        probe_registry=probe_registry, recall_port=recall_port,
        nonprod=nonprod, tolerant_patch=cfg.tolerant_patch)
    round_state = RoundState()

    def _on_step() -> None:
        """F1：每轮工具执行结束后做一次出生检测（追授后自动 no-op）。"""
        meta.effective_round_cap = budget.effective_round_cap
        if not cfg.verify_repair_tail or budget.tail_granted:
            return
        if _entrypoint_ready(workdir, ENTRYPOINT):
            budget.mark_born(budget.rounds_used)
            budget.grant_verify_tail()
            meta.tail_granted = True
            meta.phase = "verify_repair"
            meta.directive = TAIL_DIRECTIVE
            meta.effective_round_cap = budget.effective_round_cap

    def _round_context() -> dict[str, Any]:
        """每轮**读一次** scratchpad，同一份文本喂模型请求面与指纹面。"""
        text, truncated = _read_scratchpad(workdir, enabled=cfg.scratchpad)
        meta.scratchpad_text = text
        meta.scratchpad_sha256 = sha256_text(text)
        meta.scratchpad_chars = len(text)
        meta.scratchpad_truncated = truncated
        meta.effective_round_cap = budget.effective_round_cap
        return {"scratchpad_text": text, "prompt_extra": meta.directive}

    model = build_gateway_model(
        gateway, budget=budget, call_cache=call_cache,
        recorder=_make_recorder(tracer, round_state, meta),
        seq_provider=seq_provider, cache_version=cache_version,
        round_context=_round_context)
    instructions = system_contract(extra=system_extra) + _tool_docs(cfg)
    model.configure(system_prompt=instructions, task=task)

    if max_steps is not None:
        # 显式 max_steps 优先，逐字同旧版（调用方自担外壳容量）。
        steps_max = max_steps
    elif cfg.verify_repair_tail:
        # Task3 §T3.4-B：外壳必须容纳「恰好 round_cap + tail」轮模型调用。
        # 最后一轮由 budget.add_round 以 round_tail / birth_absent 终局
        # （STOP-3b：先记账后判帽），故再给 1 步——否则尾轮在 smolagents
        # 外壳层先撞 max_steps，kind 会被错记为 max_steps 而非 tail 分类。
        if budget.verify_repair_tail_rounds <= 0:
            # 开关与预算不一致会让「追授」永不可达（add_round 仍按旧版
            # round_cap 分类）；装配期 fail-closed，不静默降级。
            raise ValueError(
                "AgenticConfig.verify_repair_tail=True 但 BudgetTracker."
                "verify_repair_tail_rounds<=0：追授永不生效")
        steps_max = (max(budget.round_cap, 1)
                     + budget.verify_repair_tail_rounds + 1)
    else:
        # 全关路径逐字节同 Phase 2.5。
        steps_max = max(budget.round_cap, 1)
    condenser = Condenser(tracer)
    agent, _tools, step_counter = build_agent(
        ctx=ctx, model=model, tracer=tracer, budget=budget,
        round_state=round_state, instructions=instructions,
        max_steps=steps_max, cfg=cfg, on_step=_on_step)
    # 显式按 ActionStep 类型注册（CallbackRegistry 不支持 append）。
    agent.step_callbacks.register(ActionStep, condenser)

    kind = KIND_FINISH
    value = ""
    reason = ""
    state = ""
    try:
        result = agent.run(task, return_full_result=True,
                           max_steps=steps_max)
        state = getattr(result, "state", "")
        output = getattr(result, "output", None)
        # C3（2026-10-30 第四轮返修 MAJOR）：终局分类只认硬化执行器的
        # _terminal_sig——finish/fail 的 TerminateSignal 与三帽 CapExceeded 都
        # 是 BaseException，在工具闭包内抛出后由执行器 __call__ 捕获并存证。
        # output（含 final_answer(answer={...}) 逐字回传的 dict）是模型可写的
        # 不可信通道：其中 __agentic_terminate__/kind 标记可被逐字伪造（伪造
        # step_cap/supplier_error→自选熔断/重试路由、抑制 __answer__ 审计行），
        # 故不再作为分类依据。
        sig = getattr(getattr(agent, "python_executor", None),
                      "_terminal_sig", None)
        sig_kind = getattr(sig, "kind", None)
        if sig is not None and sig_kind in TERMINAL_SIG_KINDS:
            kind = sig_kind
            value = str(getattr(sig, "value", ""))
            reason = sig.reason
        elif sig is not None:
            # fail-closed（2026-10-30 LEAD 线审）：存在真信号但 kind 越白名单
            # ＝引擎不变量被破坏（今天五源都在集内，不可达）；显式落
            # KIND_FAIL，绝不掉到隐式 finish 放行。
            kind = KIND_FAIL
            reason = f"terminal signal kind 越白名单：{sig_kind!r}"
        elif state == "max_steps_error":
            kind = KIND_MAX_STEPS
            reason = f"smolagents max_steps={steps_max} 耗尽（未自终局）"
        else:
            # 模型直接走了上游 final_answer 而没调 finish：按隐式 finish 记录，
            # 仍然不等于通过（evaluate/Gate2 独立判定，见 graph 接线）。
            # 伪造 __agentic_terminate__ 标记在此被点名忽略（仍写 __answer__
            # 行，证据链不被模型输出抑制）。
            kind = KIND_FINISH
            value = "" if output is None else str(output)
            if isinstance(output, dict) and output.get("__agentic_terminate__"):
                reason = ("implicit_final_answer（模型输出含伪造 "
                          "__agentic_terminate__ 标记，已忽略，按隐式终局处理）")
            else:
                reason = "implicit_final_answer（未经 finish 原语）"
    except CapExceeded as exc:
        # BaseException：不被 CodeAgent 的 except Exception 包装，原样到达。
        kind = exc.kind if exc.kind in (
            KIND_STEP_CAP, KIND_TOKEN_CAP, KIND_ROUND_CAP,
            KIND_ROUND_TAIL, KIND_BIRTH_ABSENT) else KIND_FAIL
        reason = exc.reason
    except TerminateSignal as exc:  # 理论上执行器已映射；防御性兜底
        kind = exc.kind
        value = exc.value
        reason = exc.reason
    except AgentGenerationError as exc:
        # CodeAgent 把 generate 内一切 Exception 包成 AgentGenerationError
        # （from e）；解包原因链分类：供应侧错 vs 其余引擎错。
        cause: BaseException | None = exc.__cause__ or exc.__context__
        if isinstance(cause, SupplierModelError):
            kind = KIND_SUPPLIER_ERROR
            reason = str(cause)
        else:
            kind = KIND_ENGINE_ERROR
            reason = f"{type(cause).__name__ if cause else 'AgentGenerationError'}: {exc}"
    except Exception as exc:  # noqa: BLE001 - 任何残余异常优雅终局，不裸穿
        kind = KIND_ENGINE_ERROR
        reason = f"{type(exc).__name__}: {exc}"

    if kind in (KIND_FINISH,):
        tracer.record(
            seq=round_state.current_seq, round_idx=round_state.current_round,
            step=0,
            tool=ANSWER_TOOL, args={"kind": kind},
            observation=value or reason,
            policy={"decision": "allow", "reason": "",
                    "grant_source": GRANT_SOURCE_CONTROL},
            latency_ms=0, raw_response_ref=round_state.current_raw_ref)

    return AgenticOutcome(
        kind=kind, value=value, reason=reason, smolagents_state=state,
        rounds_used=budget.rounds_used, steps_used=step_counter[0],
        tokens_used=budget.tokens_used, cache_hits=model.cache_hits,
        empty_responses=model.empty_responses,
        supplier_errors=model.supplier_errors,
        trace_path=tracer.trace_path)


# ---------------------------------------------------------------------------
# 图分派缝（AC-T4-13；RunSpec.agentic 扩字段经 LEAD 裁决后由 agent_run
# 节点单点调用——新旧两引擎各在独立函数，禁止混写）
# ---------------------------------------------------------------------------

def engine_for(runspec: Any) -> str:
    """按出生契约选引擎：``"agentic"`` 或 ``"legacy"``（默认旧路径）。

    鸭子读取 ``agentic`` 属性：RunSpec 扩字段在 LEAD 裁决前不存在，
    getattr 默认 False 保证旧契约对象/None 全落旧引擎；裁决接线后
    agent_run 节点只许在此缝分支调用 run_agentic_inner_loop，旧 runner
    函数一字不改（test_task4_golden_regression 的 import 面闸锁死）。
    """
    return "agentic" if bool(getattr(runspec, "agentic", False)) else "legacy"


# ---------------------------------------------------------------------------
# kill-9 续写原语（AC-T4-13；smolagents 无官方运行期 checkpoint）
# ---------------------------------------------------------------------------

def feed_trajectory_from_trace(model: Any, tracer: TraceStore,
                               *, workdir: "str | Path",
                               budget: "BudgetTracker | None" = None
                               ) -> dict[str, Any]:
    """从 trace 全量存储重建历史观察并重放前缀链（**不重跑任何工具**）。

    同进程被 kill -9 后、图节点重跑时：新进程用本函数把 model 的轨迹前缀
    恢复到 kill 前最后一步，下一轮 generate 的指纹与原进程一致 →
    callcache 命中 → 0 新 seq。若 trace 存储不可读/被外部篡改（全量哈希
    对不上），调用方必须诚实按重出生处理（新 seq、ledger 照常记账），
    不得伪装命中。

    返回 ``{"steps_replayed": n, "round": r, "verified": bool}``。
    """
    lines = tracer.load_lines()
    rebuilt = tracer.rebuild_observations()
    n = 0
    last_round = 0
    for line in iter_trajectory(lines):
        step = int(line["step"])
        if step not in rebuilt:
            raise ValueError(
                f"续写失败：step {step} 全量观察缺失（不得重跑工具补齐）")
        text, stored_line = rebuilt[step]
        # 防篡改：重读全量文件按 bytes 对账行内原始 sha256（fail-closed）。
        raw = tracer.read_full(line["obs_full_relpath"])
        if sha256_bytes(raw) != line["obs_sha256"]:
            raise ValueError(
                f"续写失败：step {step} 全量观察哈希被篡改（fail-closed）")
        if stored_line["obs_sha256"] != line["obs_sha256"]:
            raise ValueError(
                f"续写失败：step {step} 轨迹行哈希不一致（fail-closed）")
        norm = normalize_observation(text, workdir=workdir)
        model.note_step(
            step=step, tool=line["tool"], args=line["args"],
            normalized_obs_sha256=sha256_text(norm))
        last_round = max(last_round, int(line["round"]))
        n += 1
    model.restore_round(last_round)
    if budget is not None:
        # 预算计数从 trace 重放恢复（控制行 step=0 不计工具步）。
        tool_steps = sum(1 for ln in iter_trajectory(lines))
        tokens = 0
        model_rounds = 0
        for ln in lines:
            if ln["tool"] == MODEL_ROUND_TOOL:
                model_rounds += 1
                usage = ln.get("usage")
                if isinstance(usage, list) and len(usage) == 2:
                    tokens += int(usage[0]) + int(usage[1])
        budget.rounds_used = max(last_round, model_rounds)
        budget.steps_used = tool_steps
        budget.tokens_used = tokens
    return {"steps_replayed": n, "round": last_round, "verified": True}
