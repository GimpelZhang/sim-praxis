"""S2 EvolutionSpec 机检器（计划 §2.2/§2.3；SUT 可变面 manifest 的机器可核对面）。

R2 的**反混淆铁律**（2.8.1 教训）：两臂同规同任务，唯一差异 = 变异面文件。
本模块把"什么可以变"写成机器可核对的白名单 + AST 闭包 + 越界负控：

* **白名单**：候选只允许改 ``MUTATION_SURFACE`` 三个文件（G-REPORT 守卫 /
  NC 设计技能 / 触点模板 v2）；其他任何路径（含冻结面、词表白名单、判据算法、
  统计器、任务构建器、prereg/LOCK、Gate 接线）一律 ``MUTATION_OUT_OF_SURFACE``；
* **AST 闭包**：``guards.py`` 必须是 incumbent 的超集（原有八守卫一个不少 +
  恰新增 ``g_report`` 并登记进 ``GUARDS``）；禁网络出口（socket/requests/…）、
  禁 ``eval``/``exec``/``compile``/``__import__``（模型文本永不直接当代码执行）；
* **三件套**：机制假设（非空）、可证伪预测签名（至少一个 "+" 与一个非 "+"）、
  限变异面 diff——缺一即 ``CANDIDATE_INCOMPLETE``；
* **机制门槛**：只改模板/提示词、不提供机制实现的候选被 ``NO_MECHANISM`` 拒绝
  （ShadowGate 坏候选负控的机检面）。

本模块零付费、只读；不执行候选代码（AST 静态分析）。
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["CANDIDATE_SCHEMA", "MUTATION_SURFACE", "MECHANISM_FILES",
           "FROZEN_SURFACE", "NETWORK_MODULES", "FORBIDDEN_CALLS",
           "EvolutionError", "validate_candidate", "file_sha256"]

CANDIDATE_SCHEMA = "simpraxis.rsi_s2.skill_mutation.v1"

#: 变异面（首批一类：守卫 + NC 技能 + 触点模板）；两臂唯一允许的差异。
MUTATION_SURFACE = (
    "orchestrator/hk/guards.py",
    "orchestrator/hk/nc_toolkit.py",
    "orchestrator/hk/touchpoints.py",
)
#: 机制实现文件：至少提供一个（只改模板 = 纯提示词型坏候选，须被拒）。
MECHANISM_FILES = ("orchestrator/hk/guards.py", "orchestrator/hk/nc_toolkit.py")

#: 冻结面（候选不可触碰；此处列出供负控与人类审阅对照，不在白名单内即拒）。
FROZEN_SURFACE = (
    "orchestrator/hk/contracts.py",
    "orchestrator/hk/driver.py",
    "orchestrator/hk/verdicts.py",
    "orchestrator/hk/report_contract.py",
    "orchestrator/hk/nc_registration.py",
    "orchestrator/rsi_bridge/physics_service.py",
    "orchestrator/rsi_bridge/host_recompute.py",
    "orchestrator/rsi_bridge/session_ledger.py",
    "orchestrator/rsi_bridge/review_s1.py",
    "orchestrator/rsi_bridge/review_s2.py",
    "scripts/s2/paired_analysis.py",
    "scripts/s2/propose_mutation.py",
    "orchestrator/rsi_bridge/selection.py",
    "orchestrator/rsi_r1/task_specs.py",
    "orchestrator/rsi_r1/contrasts.py",
    "orchestrator/rsi_r1/rubric.py",
    "scripts/s2/prereg_campaign.py",
)

#: 网络出口（新增即拒）。
NETWORK_MODULES = frozenset({"socket", "ssl", "http", "urllib", "urllib3",
                             "requests", "httpx", "aiohttp", "anthropic",
                             "openai", "websockets"})
#: 动态执行面（模型文本永不直接当代码执行）。
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__"})

#: 预测签名允许的方向记号。
_SIGNS = ("+", "0", "-")


class EvolutionError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}" if detail else code)


def file_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _top_level_names(tree: ast.Module) -> "set[str]":
    """顶层定义名（函数/类 + 普通赋值 + **带注解赋值** ``X: T = ...``）。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imports(tree: ast.Module) -> "set[str]":
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def _calls(tree: ast.Module) -> "set[str]":
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def _ast_check(path: str, source: str) -> ast.Module:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise EvolutionError("CANDIDATE_SYNTAX", f"{path}: {exc}") from None
    network = sorted(_imports(tree) & NETWORK_MODULES)
    if network:
        raise EvolutionError("NETWORK_EGRESS",
                             f"{path} 新增网络出口 {network}（变异面禁止）")
    forbidden = sorted(_calls(tree) & FORBIDDEN_CALLS)
    if forbidden:
        raise EvolutionError("FORBIDDEN_CALL",
                             f"{path} 调用 {forbidden}（模型文本不得直接当代码执行）")
    return tree


def _check_prediction(signature: Any) -> "dict[str, str]":
    from orchestrator.hk import report_contract as RC  # noqa: F401  (六维同源)

    from orchestrator.rsi_bridge.review_s1 import DIMENSIONS

    if not isinstance(signature, Mapping):
        raise EvolutionError("CANDIDATE_INCOMPLETE", "缺 prediction_signature")
    dims = signature.get("dimensions")
    if not isinstance(dims, Mapping) or not dims:
        raise EvolutionError("CANDIDATE_INCOMPLETE",
                             "prediction_signature.dimensions 必须非空")
    unknown = sorted(set(dims) - set(DIMENSIONS))
    if unknown:
        raise EvolutionError("PREDICTION_INVALID", f"未知维度 {unknown}")
    normalized: "dict[str, str]" = {}
    for dim, sign in dims.items():
        sign = str(sign)
        if sign not in _SIGNS:
            raise EvolutionError("PREDICTION_INVALID",
                                 f"{dim}={sign!r} 不是 {_SIGNS} 之一")
        normalized[str(dim)] = sign
    # 可证伪：不能"所有维度都提升"（那是不可检验的声明）
    if all(sign == "+" for sign in normalized.values()):
        raise EvolutionError(
            "PREDICTION_NOT_FALSIFIABLE",
            "预测签名至少需要一个非 '+' 维度（提升必须集中在机制解释的维度）")
    return dict(sorted(normalized.items()))


def _check_guards_superset(source: str, incumbent_source: str | None) -> None:
    """guards.py 必须是 incumbent 的超集 + 恰新增 g_report 并登记 G-REPORT。"""
    tree = _ast_check("orchestrator/hk/guards.py", source)
    names = _top_level_names(tree)
    if "g_report" not in names:
        raise EvolutionError("GUARD_MISSING", "候选 guards.py 未提供 g_report")
    if "G-REPORT" not in source:
        raise EvolutionError("GUARD_UNREGISTERED",
                             "候选 guards.py 未把 G-REPORT 登记进 GUARDS")
    if incumbent_source is None:
        return
    incumbent = ast.parse(incumbent_source, filename="incumbent/guards.py")
    missing = sorted(_top_level_names(incumbent) - names)
    if missing:
        raise EvolutionError(
            "GUARD_REGRESSION",
            f"候选 guards.py 丢失既有顶层定义 {missing}（只允许新增一个守卫）")


def _check_touchpoints(source: str) -> None:
    tree = _ast_check("orchestrator/hk/touchpoints.py", source)
    names = _top_level_names(tree)
    if "TOUCHPOINT_INSTRUCTIONS_V2" not in names:
        raise EvolutionError("TEMPLATE_MISSING",
                             "候选 touchpoints.py 未提供 TOUCHPOINT_INSTRUCTIONS_V2")
    text = source
    for needle in ("negative_control", "NOT_DETECTED", "report"):
        if needle not in text:
            raise EvolutionError(
                "TEMPLATE_INCOMPLETE",
                f"模板 v2 未覆盖 {needle!r}（K1 NC 设计 / K8 五值词表与六段报告）")


def _resolve_content(item: Mapping[str, Any], base_dir: Path | None) -> str:
    """文件内容：优先内联 ``content``；否则从 ``base_dir/<file>`` 读取（proposer
    落盘目录）。两者都给且不一致即 ``CANDIDATE_SHAPE``（防双写漂移）。"""
    inline = item.get("content")
    name = item.get("file")
    if isinstance(inline, str) and inline.strip() and not name:
        return inline
    if name:
        if base_dir is None:
            raise EvolutionError("CANDIDATE_SHAPE", "file 引用缺 base_dir")
        path = Path(base_dir) / Path(str(name)).name
        if not path.is_file():
            raise EvolutionError("CANDIDATE_SHAPE", f"缺文件 {path}")
        disk = path.read_text(encoding="utf-8")
        if isinstance(inline, str) and inline.strip() and inline != disk:
            raise EvolutionError("CANDIDATE_SHAPE",
                                 f"{name} 内联内容与落盘不一致")
        return disk
    if not isinstance(inline, str) or not inline.strip():
        raise EvolutionError("CANDIDATE_SHAPE",
                             f"{item.get('path')!r} 内容为空")
    return inline


def validate_candidate(package: Any, *,
                       incumbent_root: Path | None = None,
                       base_dir: Path | None = None) -> "dict[str, Any]":
    """机检候选包；任何越界/缺件/坏签名即 ``EvolutionError``（fail-closed）。"""
    if not isinstance(package, Mapping):
        raise EvolutionError("CANDIDATE_SHAPE", "候选包必须是对象")
    if package.get("schema") != CANDIDATE_SCHEMA:
        raise EvolutionError("CANDIDATE_SHAPE",
                             f"schema={package.get('schema')!r}")
    hypothesis = str(package.get("mechanism_hypothesis") or "").strip()
    if len(hypothesis) < 12:
        raise EvolutionError("CANDIDATE_INCOMPLETE",
                             "机制假设为空或过短（需机制解释，不是转写意见）")
    prediction = _check_prediction(package.get("prediction_signature"))
    files = package.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)) or not files:
        raise EvolutionError("CANDIDATE_INCOMPLETE", "缺 files（限变异面 diff）")
    seen: "dict[str, str]" = {}
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise EvolutionError("CANDIDATE_SHAPE", f"files[{index}] 不是对象")
        path = str(item.get("path") or "")
        if path not in MUTATION_SURFACE:
            raise EvolutionError(
                "MUTATION_OUT_OF_SURFACE",
                f"{path!r} 不在变异面 {list(MUTATION_SURFACE)}（冻结面/词表/判据不可触碰）")
        content = _resolve_content(item, base_dir)
        if path in seen:
            raise EvolutionError("CANDIDATE_SHAPE", f"{path} 重复")
        seen[path] = content
    if not set(seen) & set(MECHANISM_FILES):
        raise EvolutionError(
            "NO_MECHANISM",
            "只改模板/提示词而不提供机制实现（guards.py 的 G-REPORT 或 "
            "nc_toolkit.py）——ShadowGate 坏候选特征")
    incumbent_guards = None
    if incumbent_root is not None and "orchestrator/hk/guards.py" in seen:
        path = Path(incumbent_root) / "orchestrator" / "hk" / "guards.py"
        if path.is_file():
            incumbent_guards = path.read_text(encoding="utf-8")
    if "orchestrator/hk/guards.py" in seen:
        _check_guards_superset(seen["orchestrator/hk/guards.py"], incumbent_guards)
    if "orchestrator/hk/touchpoints.py" in seen:
        _check_touchpoints(seen["orchestrator/hk/touchpoints.py"])
    if "orchestrator/hk/nc_toolkit.py" in seen:
        _ast_check("orchestrator/hk/nc_toolkit.py",
                   seen["orchestrator/hk/nc_toolkit.py"])
    return {
        "schema": f"{CANDIDATE_SCHEMA}.validated",
        "mechanism_hypothesis": hypothesis,
        "prediction_signature": prediction,
        "files": sorted(seen),
        "files_sha256": {path: file_sha256(text) for path, text in sorted(seen.items())},
        "diff_sha256": hashlib.sha256(
            "".join(f"{path}\0{file_sha256(text)}\n"
                    for path, text in sorted(seen.items())).encode("utf-8")).hexdigest(),
        "validated": True,
    }
