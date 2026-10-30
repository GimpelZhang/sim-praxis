"""硬化的 LocalPythonExecutor（AC-T4-7 第一重：CodeAgent 执行器层）。

上游声明（local_python_executor.py 类 docstring 原文）：
``It is not a security sandbox: for isolated execution of untrusted code,
use a remote executor.`` 故安全语义只由我方提供，做两件上游没有的事：

1. **静态 AST 拒绝**（执行前整树 walk，包括函数体/try 分支等运行期才
   走到的嵌套节点）：全部 import 零授权（上游默认还放 11 个标准库，
   我们只收紧不放宽；需要的 math 等已在 BASE_PYTHON_TOOLS）、dunder
   Name/Attribute 全拒（``__builtins__``/``__class__``/``__subclasses__``
   逃逸链）、Global/Nonlocal 拒、危险内建名调用拒（open/eval/exec/
   compile/globals/locals/input/breakpoint/__import__/memoryview——
   上游虽因名字不可达已挡住，这里在 AST 层双闸明示拒绝）、
   ``str.format``/``format_map`` 无条件全拒（格式字段是运行期 dunder
   遍历面，AST 不可见、拼接不可判，C1 评审实证零工具可读宿主环境变量；
   模型字符串能力走 f-string/% 格式化/拼接）。静态语法点之外，getattr
   动态取这两个方法是同族绕闸面——运行期 hardened_getattr 包装上游
   nodunder_getattr 权威拦截（getattr(s,"for"+"mat") 拼接同样拦），
   静态层对 getattr 字面量第二参给早失败，普通 getattr 行为逐字不变。
2. **终局信号映射**：工具/预算层抛 ``TerminateSignal``/``CapExceeded``
   （均 BaseException，模型代码的 except Exception 接不住）时映射为
   is_final_answer 的 CodeOutput，让 CodeAgent 正常收尾；终局 kind 由
   loop 从 trace/输出 dict 分类，**finish 不等于通过**（AC-T4-6）。
   C4（第五轮返修）：``__call__`` 再加第三 clause ``except BaseException``
   ——模型 blob 若 raise SystemExit/KeyboardInterrupt/GeneratorExit/裸
   BaseException（非 ``Exception`` 家族），上游 blob 边界
   （local_python_executor.py 的 ``except FinalAnswerException`` 与
   ``except Exception``）与 CodeAgent/loop 的 ``except Exception`` 地板全接
   不住，会以裸异常穿出整个栈（SystemExit 甚至直接决定进程退出码，实测
   rc=7），绕开 FAILURE_JSON 记账。第三 clause 把它们 fail-closed 合成为
   kind=engine_error 的 TerminateSignal，0 裸穿；``Exception`` 家族（如
   blob 内普通错误被上游包成的 InterpreterError）在 clause 内原样 ``raise``，
   继续走 CodeAgent 的 AgentExecutionError 观察自愈回路（ValueError 对照钉死：
   非终局，走到轮帽而非熔断）。地板只铺在不可信 blob 缝
   （本执行器 ``__call__``），loop 宿主侧不铺 ``except BaseException``：
   宿主代码自身的这类错误必须大声崩出，不被伪装成引擎错误。落在 blob 执行
   窗口内的真实 Ctrl-C 会映射成 engine_error（无人值守批次的既定取舍：宁可
   一票 FAILURE 件也不让进程消失）；网关调用等宿主窗口的 SIGINT 仍原样裸
   出，SIGTERM/SIGKILL/看门狗始终是操作员杠杆。
   附（第五签 MINOR#1）：blob 内手写 ``except BaseException: finish(...)``
   **吞不掉**真 BaseException 家族——上游模拟解释器分派 try 处理器的外层只
   有 ``except Exception``（local_python_executor.py:1169），处理器不触发、
   finally 照跑，异常原样到达本第三 clause。

第二重（工具 forward 内 jail→schema→policy）在 tools/ 各件，pen-test
载荷表对两重分别断言拒绝。
"""
from __future__ import annotations

import ast
from typing import Callable

from smolagents.local_python_executor import (
    CodeOutput,
    InterpreterError,
    LocalPythonExecutor,
    nodunder_getattr,
)

from .caps import CapExceeded, KIND_ENGINE_ERROR

# 危险裸名：上游名字解析对未在 state/工具表内的名字直接报错（双闸之一），
# 这里在 AST 层提前明示拒绝，保证观察文本是安全拒绝而非“未定义”。
_DANGEROUS_NAMES = frozenset({
    "open", "eval", "exec", "compile", "globals", "locals",
    "input", "breakpoint", "__import__", "memoryview", "exit", "quit",
})
# 上游 ALLOWED_DUNDER_METHODS = __init__/__str__/__repr__ 仅用于模型自定义
# 类的方法定义（FunctionDef 名，不经过 Attribute 访问节点）；静态层对
# Name/Attribute 上的 dunder 一律拒绝，方法定义不拦。
#
# C1（2026-10-30 评审 CRITICAL 返修）：str.format/format_map 的格式字段在
# 运行期做属性/下标遍历，而字段藏在字符串字面量里——AST 上看不到
# __globals__/__class__ 节点，动态拼接（"{0.__glo"+"bals__}"）还能绕过
# 任何「只拒含 __ 字面量」的窄判。实证零工具逃逸：
#   "{0.__globals__[logging].os.environ[ANTHROPIC_CANARY_SECRET]}".format(f)
# 可直接读出宿主环境变量。故对 .format/.format_map **无条件全拒**（连
# 普通 "{x}".format 也拒，白名单 f-string/% 格式化/拼接不受影响）。
# 已核验 smolagents 1.26.0 注入/工具调用代码零 .format( 依赖（全包 grep
# 0 命中），且静态校验只作用于模型 code_action blob，框架自身代码不过本闸。
_FORMAT_DENY_ATTRS = frozenset({"format", "format_map"})

# C2（2026-10-30 评审 CRITICAL 返修）：引擎活对象敏感属性名。模型代码 blob
# 内即使经别名/下标/拼接拿到引擎对象（工具闭包、frozen ToolContext、模型缝
# 或其 safer_func wrapper），也不得读到这些活句柄——读是 fn-forged-ctx
# （read_file.fn(FakeCtx()) 绕 jail）与 model.gateway 逃付费（H）的原语，
# 写是 ctx.jail 伪造（A/C/K）、预算置空（B/I）、trace 抑制（D/E3）的原语。
_SENSITIVE_ATTRS = frozenset({
    "fn", "ctx", "model", "tracer", "step_counter", "control",
    "gateway", "budget", "call_cache", "recorder", "seq_provider",
    "jail", "grants", "workdir", "record",
    "_system_prompt", "_prefix", "_task",
})

# 引擎对象/函数所属模块前缀；safer_func 的 @wraps 会把 wrapper 的 __module__
# 复制成被包函数的 __module__，故工具闭包（orchestrator.agentic.loop）经
# 包装后仍被本判定命中。
_ENGINE_MODULE_PREFIX = "orchestrator.agentic."

_NATIVE_SETATTR = setattr  # 模块装配期取内建 setattr（注入后 blob 只见 hardened）。


def _is_engine_object(obj) -> bool:
    """对象本身或其 safer_func 包装目标是否属于 orchestrator.agentic.*。

    函数读实例级 ``__module__``（types.FunctionType 的 __module__ 是函数
    自己的值，不是 type(obj).__module__ 的 'builtins'）；普通实例回退到类
    模块。另对 @wraps 产生的 wrapper 跳一层 ``__wrapped__``（经 vars 取，
    不触发我们自己对 dunder 的静态限制——本函数运行在宿主侧）。
    """
    mod = getattr(obj, "__module__", "")
    if isinstance(mod, str) and mod.startswith(_ENGINE_MODULE_PREFIX):
        return True
    try:
        wrapped = vars(obj).get("__wrapped__")
    except TypeError:
        wrapped = None
    if wrapped is not None:
        wmod = getattr(wrapped, "__module__", "")
        if isinstance(wmod, str) and wmod.startswith(_ENGINE_MODULE_PREFIX):
            return True
    return False


def _format_denied_error(attr: str) -> "InterpreterError":
    return InterpreterError(
        f"静态安全闸：禁止 .{attr} 调用——str.format 字段可做 dunder 遍历"
        "（'{0.__globals__[os].environ...}'.format(f) 零工具逃逸面，"
        "动态拼接不可静态判定，一律全拒）；请用 f-string、% 格式化或拼接")


def _getattr_format_denied_error(name: str) -> "InterpreterError":
    # C1 补修（2026-10-30 LEAD 对抗探针）：getattr 动态取 format/format_map
    # 是 .format 语法点之外的同族逃逸面，错误信息与 C1 同族并点名 getattr 绕闸。
    return InterpreterError(
        f"安全闸：禁止 getattr(_, {name!r})——动态获取 str.format/format_map "
        "可做格式字段 dunder 遍历（getattr(s, 'for'+'mat') 拼接同样拦）；"
        "请用 f-string、% 格式化或拼接")


def _sensitive_attr_error(verb: str, name: str) -> "InterpreterError":
    return InterpreterError(
        f"安全闸：禁止 {verb}(_, {name!r})——引擎活对象的句柄/状态属性"
        "（ctx/model/tracer/budget/gateway/jail 等）不对模型代码开放"
        "（C2：读取=伪造上下文/逃付费原语，写入=jail 伪造/预算置空/trace"
        "抑制原语）；需要能力只能走授予工具")


def hardened_getattr(obj, name, default=None):
    """运行期 getattr 闸（C1 格式族 + C2 活对象句柄，权威层）。

    上游 ``nodunder_getattr`` 只拦 dunder 形态的 name，放行
    ``getattr(s, "format")`` 与一切非 dunder 活属性。本函数**逐字委托上游
    语义**（dunder 判定沿用上游、三参 default 行为不变、name 非 str 时仍由
    上游抛 AttributeError），仅在 dunder 判定之外追加：
    format/format_map 无条件拒（C1）；引擎对象的敏感活属性名拒（C2）。
    经 additional_functions 注入覆盖 BASE_PYTHON_TOOLS["getattr"]
    （上游合并序：additional 最后）。
    """
    if isinstance(name, str):
        if name in _FORMAT_DENY_ATTRS:
            raise _getattr_format_denied_error(name)
        if name in _SENSITIVE_ATTRS and _is_engine_object(obj):
            raise _sensitive_attr_error("getattr", name)
    return nodunder_getattr(obj, name, default)


def hardened_setattr(obj, name, value):
    """运行期 setattr 闸（C2，权威层）：上游 BASE_PYTHON_TOOLS 是裸 setattr。

    - dunder 属性名一律拒（上游 setattr 无 nodunder 对应保护，模型代码可
      ``setattr(x, '__class__', ...)``，这里补齐与 getattr 同族的 dunder 拒）；
    - 对引擎对象（orchestrator.agentic.* 含其 safer_func wrapper）写敏感
      活属性名一律拒——``setattr(finish.ctx,'jail',Fake())``（K）、
      ``setattr(r,'ctx',x)``（别名 r=read_file 形态）等。
    其余逐字走内建 setattr（模型自定义类的普通属性不受影响，反向回归）。
    """
    if isinstance(name, str):
        if name.startswith("__") and name.endswith("__"):
            raise InterpreterError(
                f"安全闸：禁止 setattr(_, {name!r})——dunder 属性改写是逃逸链"
                "形态；需要状态请用模型自定义普通属性")
        if name in _SENSITIVE_ATTRS and _is_engine_object(obj):
            raise _sensitive_attr_error("setattr", name)
    return _NATIVE_SETATTR(obj, name, value)


def _native_final_answer(answer=None, *args, **kwargs):
    """无属性裸闭包版 final_answer（C3 MINOR 返修）。

    上游 evaluate_python_code 每轮（local_python_executor.py:1630-1636）取
    ``static_tools["final_answer"]`` 包成抛 FinalAnswerException 的闭包：
    ``raise FinalAnswerException(previous_final_answer(*args, **kwargs))``——
    富 ``FinalAnswerTool`` 实例**从不面向模型 blob**，previous 只需要「逐字
    回传 answer」。故上游 BASE_PYTHON_TOOLS 的富实例在此被替换为本模块级
    裸闭包（``__dict__`` 恒空、无 from_code/save 等 Tool 面），终局链逐字
    等价而 C2 句柄面不为 final_answer 重新开口。
    """
    return answer


class TerminateSignal(BaseException):
    """finish/fail 自终局信号（BaseException：模型代码 except Exception 接不住）。"""

    def __init__(self, kind: str, reason: str, value: str = ""):
        self.kind = kind
        self.reason = reason
        self.value = value
        super().__init__(f"{kind}: {reason}")


def _chain_root_name(node: ast.AST) -> "ast.Name | None":
    """属性链最左根 Name：a.b.c → a；getattr 调用等由调用方自行判根。"""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    return cur if isinstance(cur, ast.Name) else None


class _StaticSecurityValidator(ast.NodeVisitor):
    """整树静态拒绝；任何违例抛 InterpreterError（CodeAgent 会转执行错误观察）。

    C2 返修：构造时给入本次执行器的工具名集合。模型代码对工具的**唯一合法
    用法是直接名字调用**（``read_file(path=...)``）；任何把工具名放在属性
    链根上的节点（任意深度、读/写/删/增赋值）一律静态拒——别名形态
    （``r=read_file; r.ctx=...``）静态看不到根，由「闭包 __dict__ 为空 +
    运行期 hardened setattr/getattr」兜住。
    """

    def __init__(self, tool_names: "frozenset[str] | set[str]" = frozenset()):
        super().__init__()
        self.tool_names = frozenset(tool_names)

    def visit_Import(self, node: ast.Import) -> None:
        names = ", ".join(a.name for a in node.names)
        raise InterpreterError(
            f"静态安全闸：agentic 内环零 import 授权（收到 import {names}）；"
            "需要的能力必须走六组授予工具")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        raise InterpreterError(
            f"静态安全闸：agentic 内环零 import 授权（收到 from {node.module} "
            "import ...）；需要的能力必须走六组授予工具")

    def visit_Global(self, node: ast.Global) -> None:
        raise InterpreterError("静态安全闸：禁止 global 声明")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        raise InterpreterError("静态安全闸：禁止 nonlocal 声明")

    def visit_Delete(self, node: ast.Delete) -> None:
        # C2：del read_file.x / del finish.model.tracer 同样是活对象写面。
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                root = _chain_root_name(target)
                if root is not None and root.id in self.tool_names:
                    raise InterpreterError(
                        f"静态安全闸：禁止在工具名 {root.id} 上做属性删除"
                        f"（.{target.attr}）——工具只允许直接名字调用，"
                        "其活句柄不开放")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise InterpreterError(
                f"静态安全闸：禁止 dunder 属性访问 .{node.attr}（逃逸链形态）")
        # C1（2026-10-30 评审 CRITICAL 返修）：即便只是取属性不调用
        # （g = "{}".format; g(f) 的别名形态），也在此拒掉——属性节点仍然
        # 可见，无需任何字面量窄判。
        if node.attr in _FORMAT_DENY_ATTRS:
            raise _format_denied_error(node.attr)
        # C2：工具名根的属性链（任意深度、读/写/增改）一律拒。NodeVisitor
        # 对 Assign/AugAssign/AnnAssign 目标里的 Attribute 同样走本访问器，
        # 故存储/删除上下文无需在此区分。
        root = _chain_root_name(node)
        if root is not None and root.id in self.tool_names:
            raise InterpreterError(
                f"静态安全闸：禁止访问工具 {root.id} 的属性 .{node.attr}"
                "——工具只允许直接名字调用；fn/ctx/model/tracer/step_counter"
                "等活句柄不对模型代码开放（C2 能力逃逸面）")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        name = node.id
        if name.startswith("__") and name.endswith("__"):
            raise InterpreterError(
                f"静态安全闸：禁止 dunder 名字 {name}（__builtins__ 逃逸形态）")
        if name in _DANGEROUS_NAMES:
            raise InterpreterError(
                f"静态安全闸：禁止危险内建调用 {name}（请走授予工具）")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # 直接危险名调用在 visit_Name 已拦；这里再拦属性形
        # （obj.exec(...) 一类，exec 作为普通属性不在危险模块白名单内，
        # 双闸之一是上游 DANGEROUS_FUNCTIONS，这里补静态明示）。
        if isinstance(node.func, ast.Attribute):
            # C1：.format/.format_map 无条件全拒（理由见模块级常量注释）。
            if node.func.attr in _FORMAT_DENY_ATTRS:
                raise _format_denied_error(node.func.attr)
            if node.func.attr in {
                    "system", "popen", "exec", "eval", "compile", "__import__"}:
                raise InterpreterError(
                    f"静态安全闸：禁止危险属性调用 .{node.func.attr}")
        elif isinstance(node.func, ast.Name) and node.func.id in (
                "getattr", "setattr") and len(node.args) >= 2:
            verb = node.func.id
            # C1 补修（defense-in-depth，运行期 hardened_getattr 是权威）：
            # getattr(obj, "format") 的**字面量**第二参在此早失败；第二参
            # 是变量/拼接等非字面量时静态不判，交由运行期闸兜住。
            first, second = node.args[0], node.args[1]
            if (verb == "getattr" and isinstance(second, ast.Constant)
                    and isinstance(second.value, str)
                    and second.value in _FORMAT_DENY_ATTRS):
                raise _getattr_format_denied_error(second.value)
            # C2：getattr/setattr(工具名, "fn"/"ctx"/...) 字面量敏感名早失败
            # （非字面量/别名由运行期 hardened_* 权威拦截）。
            if (isinstance(first, ast.Name) and first.id in self.tool_names
                    and isinstance(second, ast.Constant)
                    and isinstance(second.value, str)):
                attr = second.value
                if attr in _SENSITIVE_ATTRS or (
                        attr.startswith("__") and attr.endswith("__")):
                    raise _sensitive_attr_error(verb, attr)
        self.generic_visit(node)


class HardenedLocalPythonExecutor(LocalPythonExecutor):
    """零 import、AST 静态拒绝、终局信号映射的本地执行器。"""

    def __init__(self, *, max_print_outputs_length: int | None = None,
                 timeout_seconds: int | None = 30,
                 tool_invokers: "dict[str, Callable] | None" = None):
        # 显式空授权：上游仍保留 BASE_BUILTIN_MODULES 并集，静态 walk 在其
        # 之前全拒 import，双闸顺序：静态先死，运行期闸兜底。
        # C1/C2 补修：additional_functions 在上游 static_tools 合并序最后
        # （{**tools, **BASE_PYTHON_TOOLS, **additional_functions}，
        # send_tools 同序），故覆盖 BASE_PYTHON_TOOLS["getattr"]＝
        # nodunder_getattr 与裸 setattr，封死 getattr(s,"format") 动态绕闸
        # 与引擎活对象敏感属性改写。
        # C2：tool_invokers 是「工具名 → 无属性裸闭包」映射（loop 装配）。
        # CodeAgent 每步 send_tools 传入的是富属性 BoundTool（prompt schema
        # 需要），这里按名替换成裸闭包——safer_func 的 @wraps 即便包一层，
        # 复制的也是空 __dict__，模型代码读不到 fn/ctx/model/tracer。
        self._tool_invokers: dict[str, Callable] = dict(tool_invokers or {})
        # C3：最近一次 __call__ 的真终局信号（None=非终局）。loop 分类只信
        # 这个引擎来源，不信 output dict 里的 __agentic_terminate__ 标记
        # （模型可经 final_answer(answer={...}) 逐字伪造该 dict）。
        self._terminal_sig = None
        super().__init__(
            additional_authorized_imports=[],
            max_print_outputs_length=max_print_outputs_length,
            additional_functions={"getattr": hardened_getattr,
                                  "setattr": hardened_setattr},
            timeout_seconds=timeout_seconds)

    def send_tools(self, tools: "dict[str, object]") -> None:
        """C2：BoundTool 永不进 static_tools，按名替换为无属性裸闭包。

        CodeAgent（agents.py 每步）传入 ``{**self.tools, **managed_agents}``
        ——值是富属性 Tool 实例（fn/ctx/model/tracer/step_counter 全挂在
        实例上）。闭包映射里有的名字按名替换；``final_answer`` 也换成无属性
        裸闭包（C3 MINOR）；其余名字 fail-closed（managed_agents 本产品恒空，
        未来出现必须先经本闸授权），再交上游合并 BASE_PYTHON_TOOLS/additional。
        """
        replaced: dict[str, object] = {}
        for name, value in tools.items():
            if name in self._tool_invokers:
                replaced[name] = self._tool_invokers[name]
            elif name == "final_answer":
                # C3 MINOR：终局原语也只放无属性裸闭包（真正的异常包装由
                # 上游 evaluate_python_code:1630-1636 每轮自己做）。
                replaced[name] = _native_final_answer
            else:
                # C3 MINOR fail-closed：未来 managed_agents/未知工具名一律
                # 不进 static_tools（旧实现原样透传是未验证假设）。
                raise InterpreterError(
                    f"安全闸：send_tools 收到未授权工具名 {name!r}——执行器"
                    "只接受六组授予工具（按名替换为无属性裸闭包）与"
                    " final_answer；未知 managed agent/工具 fail-closed（C3）")
        super().send_tools(replaced)
        # defense-in-depth：smolagents 1.26.0 的 BASE_PYTHON_TOOLS 实测不含
        # final_answer（CodeAgent 侧注入），本行在当前版本冗余；保留是防上游
        # 未来在 BASE 加入同名富工具后合并序盖掉上面的裸闭包。
        # evaluate_python_code 每轮 .copy() 后再包 FinalAnswerException 闭包。
        self.static_tools["final_answer"] = _native_final_answer

    def static_validate(self, code_action: str) -> ast.AST:
        try:
            tree = ast.parse(code_action)
        except SyntaxError as exc:
            raise InterpreterError(
                f"静态安全闸：代码语法不可解析（行 {exc.lineno}）：{exc.msg}")
        _StaticSecurityValidator(self._tool_invokers.keys()).visit(tree)
        return tree

    def __call__(self, code_action: str) -> CodeOutput:
        # C3：入口第一行复位，确保 _terminal_sig 只反映最近一次执行——任何
        # 异常路径（含静态校验抛错）都不留陈旧槽。loop 分类只读这个引擎来源，
        # 不读 output dict 里的可伪造标记。
        self._terminal_sig = None
        self.static_validate(code_action)
        try:
            return super().__call__(code_action)
        except TerminateSignal as sig:
            self._terminal_sig = sig
            return self._termination_output(sig)
        except CapExceeded as sig:
            self._terminal_sig = sig
            return self._termination_output(sig)
        except BaseException as exc:
            # C4：clause 顺序固定为 TerminateSignal→CapExceeded→BaseException
            # （前两者是上面两个 clause 的子类以外的同族，必须先按精确类型
            # 收容）。SystemExit/KeyboardInterrupt/GeneratorExit/裸 BaseException
            # 不是 Exception，上游 blob 边界与 loop 的 except Exception 全接不
            # 住，会裸穿出栈（SystemExit 还会决定进程退出码）。只在此不可信
            # blob 缝铺地板，fail-closed 合成 engine_error 终局信号；loop 宿主
            # 侧刻意不铺 except BaseException（宿主自身的此类错误必须大声崩）。
            # 落在 blob 窗口内的真实 Ctrl-C 同样映射 engine_error——无人值守
            # 批次既定取舍（见模块 docstring 第 2 点与 C4 commit body）。
            if isinstance(exc, Exception):
                # Exception 家族必须原样上抛：blob 内普通错误已被上游
                # evaluate_python_code 的 ``except Exception`` 包成
                # InterpreterError，CodeAgent._step 的 ``except Exception``
                # （agents.py:1729）再包成 AgentExecutionError 观察喂回模型，
                # 这是 ValueError 等错误的「观察→继续→轮帽」自愈回路；在此吞
                # 掉会把普通 blob 错误误判成 engine_error（C4 ValueError 对照
                # 测试钉死：非终局，走到轮帽而非熔断）。
                raise
            sig = TerminateSignal(
                kind=KIND_ENGINE_ERROR,
                reason=(
                    f"blob 抛出非 Exception 基类异常 {type(exc).__name__}: "
                    f"{exc}（SystemExit/KeyboardInterrupt/GeneratorExit/"
                    "BaseException 不被上游 except Exception 收容，C4 "
                    "fail-closed 映射 engine_error，0 裸穿）"))
            self._terminal_sig = sig
            return self._termination_output(sig)

    def _termination_output(self, sig: "TerminateSignal | CapExceeded") -> CodeOutput:
        logs = str(self.state.get("_print_outputs", ""))
        output = {
            "__agentic_terminate__": True,
            "kind": sig.kind,
            "reason": sig.reason,
            "value": getattr(sig, "value", ""),
        }
        return CodeOutput(output=output, logs=logs, is_final_answer=True)
