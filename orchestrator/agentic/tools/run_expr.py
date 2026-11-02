"""F2：内联探针（受限小程序）。AST 白名单 + 容器内执行，零 host eval。

形态（``mode="exec"``）：``(import 语句 | 表达式语句)*``——**除此之外没有
任何语句类型**。不开 ``mode="eval"``——eval 模式语法上不可能出现 import，
``expr_modules`` 授予会变成死代码（Phase 2.6 计划 §T3.4-C 注①，附录 C 第 19
条锁内适配）。

**边界声明（计划附录 C 第 52 条，实施者不得删除）**：本校验器是「防误用
闸」，不是沙箱逃逸边界。规则 1–10 保证的是「模型写的表达式只做语法白名单
内的事、且不会误碰 dunder 与私有属性」，**不**保证「无法从白名单模块里取
到系统能力」——``import os.path as path`` 后 ``path.os`` 仍指向整个 ``os``，
``numpy.ctypeslib`` 同理，``path.os.system("id")`` 能穿过规则 3/5/7/10。
这不是缺陷也不是放水：真正的逃逸边界是容器（``--network=none``、
``--read-only``、``--cap-drop=ALL``、``--user 1000:1000``、``--pids-limit``，
见 ``run.py`` 的 ``build_docker_create_argv``），``run_expr`` 只是把「写临时
脚本 + run_cmd」收敛成一条可审计的窄路径。若未来要真封闭，须另立预注册
任务（属性白名单 + ``path.os`` 恶意样例入集），不在本 Task 顺手加。
"""
from __future__ import annotations

import ast
from typing import Any

from ..policy import PolicyDecision, PreToolUsePolicy, ToolContext
from ..schema import Field, SchemaError, validate
from . import ToolResult
from .run import check_backend, execute_render

MAX_EXPR_CHARS = 4000
EXPR_TIMEOUT_S = 300

# 允许出现的 AST 节点类型（白名单；集外即拒）。
# ast.Store 是推导式目标 ``[c for c in ...]`` 的 ctx 节点，属表达式形态本身
# 的一部分（计划清单漏列；不列它会把合法推导式判成 node_not_allowed:Store）。
_ALLOWED_NODES = frozenset({
    ast.Module, ast.Expr, ast.Constant, ast.Name, ast.Load, ast.Store,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Call, ast.keyword, ast.Attribute, ast.Subscript, ast.Slice,
    ast.Tuple, ast.List, ast.Dict, ast.Set, ast.Starred,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.JoinedStr, ast.FormattedValue,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot,
    ast.Import, ast.ImportFrom, ast.alias,
})
_BUILTINS = frozenset({
    "print", "len", "range", "enumerate", "zip", "sorted", "round", "abs",
    "min", "max", "sum", "isinstance", "hasattr", "getattr", "repr", "dir",
    "str", "int", "float", "bool", "list", "dict", "set", "tuple",
})
_LITERAL_ATTR_BUILTINS = frozenset({"getattr", "setattr", "hasattr", "dir"})
_STATEMENT_TYPES = (ast.Import, ast.ImportFrom, ast.Expr)


class ExprRejected(ValueError):
    """程序被白名单拒绝；reason 为十条规则之一的机械标签。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _bound_names(tree: ast.Module) -> set[str]:
    """本程序内绑定的名字：import 绑定 + 推导式目标（赋值语句本就不允许）。"""
    bound: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    continue  # 规则 8 star_import 拒绝
                bound.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            for child in ast.walk(node.target):
                if isinstance(child, ast.Name):
                    bound.add(child.id)
    return bound


def validate_program(src: str, *, allowed_modules: frozenset[str]) -> None:
    """非法即抛 :class:`ExprRejected`；返回 None 表示可执行。

    检查次序固定为 计划 §T3.4-C 的规则 1→10（先命中先返回），但顶层语句
    类型先于节点白名单检查：计划把 ``x = 1`` 归类为规则 4
    ``stmt_not_allowed``，若先做全树节点白名单，Assign 会先命中规则 3。
    表达式子树的节点白名单（规则 3）对 ``Expr`` 内的 Lambda/推导式等仍然
    逐节点生效。
    """
    if not isinstance(src, str):
        raise ExprRejected("syntax_error")           # 规则 2 的类型前提
    if len(src) > MAX_EXPR_CHARS:                    # 规则 1
        raise ExprRejected("len_exceeded")
    try:
        tree = ast.parse(src, mode="exec")           # 规则 2
    except SyntaxError:
        raise ExprRejected("syntax_error") from None

    # 规则 4（先于规则 3，理由见 docstring）
    for stmt in tree.body:
        if not isinstance(stmt, _STATEMENT_TYPES):
            raise ExprRejected(f"stmt_not_allowed:{type(stmt).__name__}")

    # 规则 3
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ExprRejected(f"node_not_allowed:{type(node).__name__}")

    # 规则 5（名字白名单；推导式目标视为本程序内绑定）
    allowed_names = _BUILTINS | _bound_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id not in allowed_names:
                raise ExprRejected(f"name_not_allowed:{node.id}")

    # 规则 6（字符串常量不得含 dunder）
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and "__" in node.value:
            raise ExprRejected("dunder_string")

    # 规则 7（私有属性）
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExprRejected("private_attr")

    # 规则 8（导入面：完整点分名，不截根；from X import * 拒绝）
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    raise ExprRejected("star_import")
            module = node.module or ""
            if module not in allowed_modules:
                raise ExprRejected(f"module_not_allowed:{module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in allowed_modules:
                    raise ExprRejected(f"module_not_allowed:{alias.name}")

    # 规则 9（点分模块必须 as 且绑定名 = 末段；alias 数恒为 1）
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        if len(node.names) != 1:
            raise ExprRejected("binding_mismatch")
        alias = node.names[0]
        if "." in alias.name:
            last = alias.name.rsplit(".", 1)[1]
            if alias.asname != last:
                raise ExprRejected("binding_mismatch")

    # 规则 10（getattr/setattr/hasattr/dir 的属性名必须是字面量）
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in _LITERAL_ATTR_BUILTINS:
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
            # 十条规则里最贴近的机械标签：该内建只允许字面量形态。
            raise ExprRejected(f"name_not_allowed:{node.func.id}")


def _check_src(value: Any) -> None:
    if not isinstance(value, str):
        raise SchemaError(f"必须是字符串，收到 {type(value).__name__}")


def run_expr(ctx: ToolContext, **kwargs) -> ToolResult:
    """执行受限小程序（复用 RunBackend；直接执行已校验源码，零拼接）。"""
    try:
        args = validate((Field("src", True, None, _check_src),), kwargs)
    except SchemaError as exc:
        return ToolResult.denied_result(PolicyDecision.deny(
            f"schema 拒绝：{exc}"))
    src: str = args["src"]
    decision = PreToolUsePolicy(
        ctx.grants, workdir=ctx.workdir, nonprod=ctx.nonprod,
        probe_registry=ctx.probe_registry).check("run_expr", args)
    if decision.decision == "deny":
        return ToolResult.denied_result(decision)
    try:
        validate_program(src, allowed_modules=ctx.grants.expr_modules)
    except ExprRejected as exc:
        return ToolResult.denied_result(PolicyDecision.deny(
            f"expr 拒绝（{exc.reason}）"))
    backend = check_backend(ctx)
    if isinstance(backend, ToolResult):
        return backend
    # 不拼接改写：校验的字节 == 执行的字节（AC-T3-2 argv[-1] == src）。
    return execute_render(ctx, ["python", "-I", "-c", src],
                          tag="run_expr", timeout_s=EXPR_TIMEOUT_S)
