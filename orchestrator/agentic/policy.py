"""PreToolUse 硬拒绝层（AC-T4-7 三硬闸之第三闸，仿 OpenHands hook exit 2）。

不是 confirm：policy 的输出只有 allow / deny(+reason)，模型拿到的是一条
拒绝观察（``[POLICY DENY exit=2] ...``），环继续但副作用零发生。授予面
来自 Gate1 工具授予表（``GrantTable``），表外一律 deny；默认构造为
全拒绝，生产接线必须显式授。

强制链在每个工具 ``forward`` 内固定为：jail（路径形态/realpath）→
schema（类型/长度）→ policy（本件）→ 执行；任一闸失败即零副作用返回
deny 观察。policy 不读模型内容以外的任何全局状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .jail import JailError, PathJail

if TYPE_CHECKING:  # 防循环导入；运行期只用鸭子类型。
    from .forensics import TraceStore
    from .tools.run import RunBackend
    from .tools.probe import ProbeRegistry
    from .tools.recall import RecallPort

# OpenHands PreToolUse hook 的 exit 2 语义：阻断，不询问。
POLICY_DENY_EXIT = 2
POLICY_DENY_MARKER = "[POLICY DENY exit=2]"

GRANT_SOURCE_GATE1 = "gate1"
GRANT_SOURCE_CONTROL = "control_primitive"
GRANT_SOURCE_DENY = "policy_deny"

# 六组工具 + 两个控制原语的全集；GrantTable.tools 只授六组。
# Phase 2.6 Task3 增两项（均为 Gate1 逐项授予）：run_expr（F2 内联探针，
# 走 run_backend，**不**并入 run_cmd 的 argv 闸——其 -c 会被
# ``_PY_DENY_FLAGS`` 判死，两者授权模型完全不同）与 update_scratchpad
# （F4 跨轮记忆写，走 write_file 同款 write-roots 闸）。
SIDE_EFFECT_TOOLS = frozenset({
    "read_file", "list_dir", "grep",
    "write_file", "apply_patch",
    "run_cmd", "run_probe", "recall",
    "run_expr", "update_scratchpad",
})
CONTROL_TOOLS = frozenset({"finish", "fail"})

# argv 列表形态下 shell 不存在，但这些字符对白名单程序无意义，只可能是
# 注入尝试（如 pytest -k "x; rm -rf /"、python -c 的偷渡变体）。
_SHELL_META = set(";|&$<>`\n\r")
# python -c 让模型代码绕过 HardenedLocalPythonExecutor 的 AST 闸在容器
# 里裸跑，默认拒绝；"python -m pip ..." 同样默认拒绝（依赖只能在镜像
# 构建期预装，AC-T4-18）。Gate1 逐项授权才可放开（本 Task 无放开路径）。
_PY_DENY_FLAGS = {"-c", "-"}
_PY_DENY_MODULES = {"pip", "ensurepip"}
# getopt 簇里携带参数的短选项：-W/-X 的参数是簇内剩余字符或下一 token，
# 其后的 c/m 不允许再按选项扫描（python 亦如此解析）。
_PY_OPTARG_FLAGS = frozenset({"W", "X"})


class PolicyError(RuntimeError):
    """policy 自身装配错误（编程期失败，不是模型触发的 deny）。"""


@dataclass(frozen=True)
class PolicyDecision:
    decision: str            # "allow" | "deny"
    reason: str
    grant_source: str

    def as_trace(self) -> dict[str, str]:
        """trace.jsonl policy_decision 字段的逐字形状。"""
        return {"decision": self.decision, "reason": self.reason,
                "grant_source": self.grant_source}

    @staticmethod
    def allow(source: str = GRANT_SOURCE_GATE1) -> "PolicyDecision":
        return PolicyDecision("allow", "", source)

    @staticmethod
    def deny(reason: str) -> "PolicyDecision":
        return PolicyDecision("deny", reason, GRANT_SOURCE_DENY)


@dataclass(frozen=True)
class GrantTable:
    """Gate1 工具授予表（AC-T4-11 的 RunSpec.tool_grants 经解析后入此）。

    - tools:        被授的六组工具名；未列即 deny。
    - run_argv:     run_cmd argv 白名单前缀（元组逐元素相等），如
                    ``("python",)``、``("pytest", "-q")``。
    - write_roots:  写允许的 workdir 相对前缀；``("",)`` 表整个 workdir。
    - probes:       允许的白名单探针名（须同时在 ProbeRegistry 内）。
    - network_probes: 允许触网的探针名（默认空；须 Gate1 逐项授权）。
    - expr_modules: run_expr 程序可 import 的模块完整点分名白名单（默认空）。
    """

    tools: frozenset[str] = field(default_factory=frozenset)
    run_argv: tuple[tuple[str, ...], ...] = ()
    write_roots: tuple[str, ...] = ()
    probes: frozenset[str] = field(default_factory=frozenset)
    network_probes: frozenset[str] = field(default_factory=frozenset)
    expr_modules: frozenset[str] = field(default_factory=frozenset)

    def write_roots_resolved(self, workdir: Path) -> list[Path]:
        roots: list[Path] = []
        wd = Path(workdir).resolve(strict=False)
        for rel in self.write_roots:
            if not isinstance(rel, str):
                raise PolicyError(f"write_roots 项必须是字符串：{rel!r}")
            if rel == "":
                roots.append(wd)
                continue
            if Path(rel).is_absolute() or ".." in Path(rel).parts:
                raise PolicyError(f"write_roots 只允许 workdir 相对路径：{rel!r}")
            roots.append((wd / rel).resolve(strict=False))
        return roots


@dataclass(frozen=True, slots=True)
class ToolContext:
    """一次 agentic run 的工具执行上下文（由 loop engine 装配，测试直接构造）。

    C2（2026-10-30 评审 CRITICAL 返修）：**frozen + slots**。模型代码 blob
    经 ``safer_func`` 包装穿透读到的工具闭包若持有本对象，任何
    ``ctx.jail = FakeJail()``/``ctx.budget = None`` 都必须失败——frozen 实例
    无 ``__dict__``、字段不可改写，从结构上封死 A/B/C/D/K 族活对象改写。
    本对象只保留工具层运行所需的**不可变引用/值**；可变的预算帽与轮态
    （current_seq/current_round/current_raw_ref）不在此对象上，分别由
    loop 装配期的闭包细胞与 :class:`~orchestrator.agentic.loop.RoundState`
    私有持有，模型代码无任何名字可达。
    """

    workdir: Path
    grants: GrantTable
    thread_id: str
    tracer: "TraceStore | None" = None
    run_backend: "RunBackend | None" = None
    probe_registry: "ProbeRegistry | None" = None
    recall_port: "RecallPort | None" = None
    nonprod: bool = False
    # Phase 2.6 Task3（F3）：仅在 AgenticConfig.tolerant_patch=True 时置真；
    # 不可变 bool，工具层据此决定 unified_diff 通道是否启用唯一命中回退。
    tolerant_patch: bool = False
    jail: PathJail = field(init=False)

    def __post_init__(self) -> None:
        # frozen dataclass 生成的 __setattr__ 恒抛 FrozenInstanceError；
        # 初始化期经 object.__setattr__ 落槽一次，之后再无改写路径。
        workdir = Path(self.workdir).resolve(strict=False)
        object.__setattr__(self, "workdir", workdir)
        object.__setattr__(self, "jail", PathJail(workdir))


class PreToolUsePolicy:
    """逐工具硬拒绝决策（无 IO，纯函数式：同参同决）。"""

    def __init__(self, grants: GrantTable, *, workdir: Path,
                 nonprod: bool = False,
                 probe_registry: "ProbeRegistry | None" = None):
        self.grants = grants
        self.workdir = Path(workdir).resolve(strict=False)
        self.nonprod = nonprod
        self.probe_registry = probe_registry

    def check(self, tool: str, validated_args: dict[str, Any],
             *, resolved_path: Path | None = None) -> PolicyDecision:
        if tool in CONTROL_TOOLS:
            # finish/fail 是控制原语，无文件/进程副作用，不经 Gate1 授予。
            return PolicyDecision.allow(GRANT_SOURCE_CONTROL)
        if tool not in SIDE_EFFECT_TOOLS:
            return PolicyDecision.deny(f"未知工具：{tool}")
        if tool not in self.grants.tools:
            return PolicyDecision.deny(
                f"工具 {tool} 不在 Gate1 授予表内（授予："
                f"{sorted(self.grants.tools) or '空'}）")
        if tool in ("write_file", "apply_patch", "update_scratchpad"):
            decision = self._check_write(
                tool, resolved_path, content=validated_args.get("content"))
            if decision.decision == "deny":
                return decision
        if tool == "run_cmd":
            decision = self._check_run_cmd(validated_args)
            if decision.decision == "deny":
                return decision
        if tool == "run_probe":
            decision = self._check_probe(validated_args)
            if decision.decision == "deny":
                return decision
        return PolicyDecision.allow()

    def _check_write(self, tool: str, resolved_path: Path | None,
                     *, content: str | None = None) -> PolicyDecision:
        if self.nonprod:
            return PolicyDecision.deny(
                "本地 NONPROD 形态 policy 拒绝一切写类工具"
                "（write_file/apply_patch 仅兄弟容器 run 形态可用，AC-T4-17）")
        if tool == "apply_patch":
            # 补丁内多文件的路径 jail/AST 闸在 tools.write 内逐件过；
            # write_roots 允许面同样适用（逐件 resolve 后复检）。
            # cad_guard（D1-5）由 tools.write._guard_staged 在 _commit_staged
            # 之前逐件过投影后的全文（本钩子只拿到 write_file 的 content，
            # 补丁文本是 diff 不是 Python，此处不扫，避免把被删行算成命中）。
            return PolicyDecision.allow()
        roots = self.grants.write_roots_resolved(self.workdir)
        if not roots:
            return PolicyDecision.deny("Gate1 未授予任何可写路径前缀")
        if resolved_path is None:
            return PolicyDecision.deny("写目标未解析")
        target = Path(resolved_path).resolve(strict=False)
        within = False
        for root in roots:
            if target == root or root in target.parents:
                within = True
                break
        if not within:
            return PolicyDecision.deny(
                f"写目标 {target} 不在 Gate1 可写前缀 "
                f"{[str(r) for r in roots]} 之内")
        # cad_guard（D1-5 / AC-T1-3）：写入**前**扫描内容，命中即 deny 观察，
        # 零副作用。只扫 .py（非 Python 文件不存在「从零建网格」的写法）。
        # 延迟导入：tools 包的 __init__ 反向依赖本模块（policy），顶层导入成环。
        if content and target.suffix == ".py":
            from .tools import cad_guard  # noqa: PLC0415
            reason = cad_guard.deny_reason(content, tier=cad_guard.TIER_MODEL)
            if reason is not None:
                return PolicyDecision.deny(reason)
        return PolicyDecision.allow()

    def _check_run_cmd(self, args: dict[str, Any]) -> PolicyDecision:
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv:
            return PolicyDecision.deny("argv 必须是非空列表（禁止 shell 字符串）")
        for seg in argv:
            if not isinstance(seg, str) or any(c in seg for c in _SHELL_META):
                return PolicyDecision.deny(
                    "argv 段含 shell 元字符或非字符串（本工具永不经过 shell）")
        head = self._argv0(argv[0])
        matched = None
        for prefix in self.grants.run_argv:
            if not prefix:
                continue
            if self._argv0(prefix[0]) == head and tuple(argv[1:len(prefix)]) == tuple(
                    prefix[1:len(prefix)]):
                matched = prefix
                break
        if matched is None:
            return PolicyDecision.deny(
                f"argv 前缀 {tuple(argv[:2])!r} 不在 Gate1 argv 白名单 "
                f"{self.grants.run_argv!r} 内")
        # 白名单内的偷渡形：python -c / python - / python -m pip。
        if head in ("python", "python3"):
            deny_reason = self._check_python_option_cluster(argv, matched)
            if deny_reason is not None:
                return PolicyDecision.deny(deny_reason)
        return PolicyDecision.allow()

    @staticmethod
    def _check_python_option_cluster(argv: list[str],
                                     matched: tuple[str, ...]
                                     ) -> "str | None":
        """按 CPython getopt 簇语义扫描 python 选项（M1 评审返修）。

        旧实现只对 argv 做整 token 集合比对，``-cprint(1)``/``-mpip``/
        ``-Smpip``/``-c=print(1)``/``"-m pip"`` 单 token 等簇形态全部绕过。
        扫描从 argv[1] 起，到首个位置参数（脚本名）为止；规则与
        ``Modules/main.c`` pymain_get_optimization 之外的 getopt 行为对齐：

        - 独立 ``--``：选项扫描结束，之后即使出现 -c 也只是脚本参数 → 放行；
        - 独立 ``-``：stdin 代码面（实证 ``echo code | python -`` 可裸跑）→ 拒；
        - 单横短选项簇逐字符扫描；遇 ``c``：簇内剩余字符（含 ``=print``
          这种）或下一 token 是命令体 → 拒（无白名单例外，代码只能走硬化
          执行器与白名单脚本）；
        - 遇 ``m``：簇内剩余或下一 token 是模块名；pip/ensurepip 及
          ``pip.`` 前缀（如 pip._internal）→ 拒；其余模块仅当显式落在
          Gate1 argv 前缀字面量内（白名单授予 ``run:python:-m:<mod>``）
          才放行——宽授予 ``run:python`` 下的 ``-mc``（可执行 cwd 内任意
          c.py）等簇形态一律拒；
        - 遇 W/X：CPython 把它视作必带参选项（getopt 名后冒号）——
          簇内剩余非空（``-Wignore``/``-Xdev``）时参数即剩余字符，不另吃
          token；簇内剩余为空（独立 ``-W``/``-X`` 是本簇末字符）时参数是
          **下一整个 token**（``-W ignore``），必须 i+=2 吃掉它再继续选项
          扫描，否则参数 token 会被当成首个位置参数使扫描提前 allow，
          后面的 ``-c x`` 偷渡（M1 残留返修，评审 m1.py 电池两例）；
        - ``--`` 开头长选项跳过；首个非选项 token 是脚本名，扫描结束
          （其后的 -c 是脚本参数，如 ``python train.py -c x``，放行）。
        """
        i = 1
        n = len(argv)
        while i < n:
            tok = argv[i]
            if not isinstance(tok, str):
                return "argv 段必须是字符串"
            if tok == "--":
                return None
            if tok == "-":
                return ("拒绝 python -：stdin 形态可绕过硬化执行器，"
                        "代码执行只能走硬化执行器与白名单脚本")
            if tok.startswith("--"):
                i += 1
                continue
            if not tok.startswith("-"):
                return None  # 首个位置参数=脚本名；其后都是脚本参数。
            cluster = tok[1:]
            j = 0
            # 本选项额外吃掉的下一 token 数（独立 -m 的模块名 / 独立 -W、-X
            # 的必带参数）；簇内参数形态为 0。
            consume_next = 0
            while j < len(cluster):
                ch = cluster[j]
                if ch == "c":
                    return ("拒绝 python -c（含组合簇 -cX/-xcX/-c=X 与"
                            "独立 -c）：代码执行只能走硬化执行器与白名单脚本")
                if ch == "m":
                    rest = cluster[j + 1:]
                    if rest:
                        module = rest
                    else:
                        # 独立 -m：模块名是下一 token（即使下一 token 形似
                        # 选项——python 同样把它当模块名）。
                        if i + 1 >= n:
                            return "拒绝 python -m：缺模块名（依赖只能镜像构建期预装）"
                        module = argv[i + 1]
                        consume_next = 1
                    mod_base = module.split(".", 1)[0]
                    if mod_base in _PY_DENY_MODULES or module in _PY_DENY_MODULES:
                        return ("拒绝 python -m pip/ensurepip（含 -mpip/-Smpip/"
                                "pip._internal 簇形态）：依赖只能镜像构建期预装"
                                "（AC-T4-18）")
                    # 非 pip 模块：仅当显式 argv 白名单前缀逐字覆盖本 -m 位置。
                    # matched 下标与 argv 对齐（matched[0]==argv[0]）；簇形态
                    # tok 不可能等于 "-m"，故一律落不到白名单 → 拒（如 -mc）。
                    if not (tok == "-m" and i + 1 < len(matched)
                            and matched[i] == "-m" and matched[i + 1] == module):
                        return (
                            f"拒绝 python -m {module}：模块执行须在 Gate1 argv "
                            "白名单内显式逐字授予（宽 python 授予不隐含 -m，"
                            "簇形态可执行 cwd 任意同名模块）")
                    break
                if ch in _PY_OPTARG_FLAGS:
                    if cluster[j + 1:]:
                        # -Wignore/-Xdev：参数是簇内剩余，不另吃 token。
                        break
                    # 独立 -W/-X（本簇末字符）：参数是下一整个 token；末位
                    # 缺参由 python 自身报错（无代码面，allow），仅在有下一
                    # token 时吃掉它，使其后的 -c 仍被本扫描看到。
                    if i + 1 < n:
                        consume_next = 1
                    break
                j += 1
            i += 1 + consume_next
        return None

    @staticmethod
    def _argv0(token: str) -> str:
        # /opt/venv/bin/python 与 python 视为同头（容器内 PATH 形态）。
        base = token.rsplit("/", 1)[-1]
        return "python" if base in ("python", "python3") else base

    def _check_probe(self, args: dict[str, Any]) -> PolicyDecision:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return PolicyDecision.deny("探针名必须是非空字符串")
        if self.probe_registry is None:
            return PolicyDecision.deny("探针注册表未装配")
        if not self.probe_registry.is_known(name):
            return PolicyDecision.deny(f"非白名单探针：{name}")
        if name not in self.grants.probes:
            return PolicyDecision.deny(f"探针 {name} 未在 Gate1 逐项授权")
        if self.probe_registry.is_network(name) and name not in self.grants.network_probes:
            return PolicyDecision.deny(
                f"网络探针 {name} 须 Gate1 逐项网络授权（默认禁网）")
        return PolicyDecision.allow()


def deny_observation(decision: PolicyDecision) -> str:
    """deny 决策转模型可见观察（exit 2 语义；零副作用已在工具层保证）。"""
    return f"{POLICY_DENY_MARKER} {decision.reason}"


def resolve_or_deny(jail: PathJail, rel: object, *, must_exist: bool = False
                    ) -> tuple[Path | None, str | None]:
    """jail 闸的小适配：成功返 (path, None)，失败返 (None, reason)。

    强制链里 jail 是第一闸，JailError 不允许冒到执行器外变裸异常
    （AC-T4-7：恶意载荷必须被『拒』并回观察，不是 500）。
    """
    try:
        return jail.resolve(rel, must_exist=must_exist), None
    except (JailError, ValueError) as exc:
        return None, f"jail 拒绝：{exc}"
