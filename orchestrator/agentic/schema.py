"""手写工具参数 schema 校验（AC-T4-7 三硬闸之第二闸；不引新包）。

pydantic 2.11.10 是既有依赖，但 agentic 工具的入参面很小且校验失败要走
统一的硬拒绝观察（exit 2 语义），故用声明式手写校验器：类型/必填/长度/
取值域在此一处定义，测试直接构造坏参断言拒绝。任何工具 forward 在碰
jail/policy 之前先过本闸（强制链顺序：jail → schema → policy，其中
路径类参数先过 jail 的形态检查再过本 schema）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# 模型给字符串的统一长度上限（防巨型参数灌满上下文/拖垮正则）。
MAX_ARG_CHARS = 200_000
MAX_PATH_CHARS = 4096
MAX_PATTERN_CHARS = 4_000


class SchemaError(ValueError):
    """参数 schema 校验失败（硬拒绝；以观察文本回模型，不抛裸异常出环）。"""


@dataclass(frozen=True)
class Field:
    name: str
    required: bool = True
    default: Any = None
    check: Callable[[Any], None] = lambda value: None


def _is_str(value: Any) -> None:
    if not isinstance(value, str):
        raise SchemaError(f"必须是字符串，收到 {type(value).__name__}")
    # bool 不是 str，但 bytes 也必须显式挡（str==bytes 混淆面）。
    if isinstance(value, bytes):  # pragma: no cover - isinstance str 已挡，防御
        raise SchemaError("必须是字符串，收到 bytes")


def _is_int(value: Any) -> None:
    # bool 是 int 子类，严口径拒绝（与 callcache usage 同纪律）。
    if type(value) is not int:
        raise SchemaError(f"必须是整数，收到 {type(value).__name__}")


def _is_list_of_str(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise SchemaError("必须是非空字符串列表")
    for item in value:
        if not isinstance(item, str) or not item:
            raise SchemaError("列表每一项都必须是非空字符串")


def str_field(name: str, *, required: bool = True, default: str | None = None,
              max_chars: int = MAX_ARG_CHARS) -> Field:
    def check(value: Any) -> None:
        _is_str(value)
        if not value and required:
            raise SchemaError("不能为空字符串")
        if len(value) > max_chars:
            raise SchemaError(f"超长：{len(value)} > {max_chars} 字符")

    return Field(name, required, default, check)


def int_field(name: str, *, required: bool = False, default: int = 0,
              minimum: int = 0, maximum: int = 10 ** 9) -> Field:
    def check(value: Any) -> None:
        _is_int(value)
        if not minimum <= value <= maximum:
            raise SchemaError(f"整数越界：{value} 不在 [{minimum}, {maximum}]")

    return Field(name, required, default, check)


def argv_field(name: str) -> Field:
    def check(value: Any) -> None:
        _is_list_of_str(value)
        if len(value) > 64:
            raise SchemaError("argv 最多 64 段")
        for item in value:
            if len(item) > 4096 or "\x00" in item:
                raise SchemaError("argv 段非法（超长或含 NUL）")

    return Field(name, True, None, check)


def validate(fields: tuple[Field, ...], kwargs: Any) -> dict[str, Any]:
    """按声明校验一次工具调用参数，返回填好默认值的 dict。

    只接受 dict（CodeAgent 以关键字参数调工具；单 dict 形态由 smolagents
    Tool.__call__ 归一）。拒未知字段（防模型塞私货参数）。
    """
    if not isinstance(kwargs, dict):
        raise SchemaError(f"参数必须是对象(dict)，收到 {type(kwargs).__name__}")
    known = {f.name for f in fields}
    unknown = set(kwargs) - known
    if unknown:
        raise SchemaError(f"未知参数：{sorted(unknown)}")
    out: dict[str, Any] = {}
    for f in fields:
        if f.name not in kwargs or kwargs[f.name] is None:
            if f.required:
                raise SchemaError(f"缺少必填参数：{f.name}")
            out[f.name] = f.default
            continue
        value = kwargs[f.name]
        f.check(value)
        out[f.name] = value
    return out
