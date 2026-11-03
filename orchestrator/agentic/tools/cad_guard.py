"""cad_guard：禁止在任务代码里「从零构建网格」的写入前闸（D1-5 / AC-T1-3）。

为什么放在**写入前**（而不是跑完之后静态检查）：模型的动作是「写文件 →
run_cmd 跑」。写出去之后再骂等于没拦，副作用已经落盘、下一轮还能直接
被别的命令读到。本模块只在内容进盘之前给出 deny 观察。

两级规则（锁内适配 L-12，Phase_2.7_Plan_Complete §1.0）：

- ``RULES_ASSEMBLY``（**永远**生效）：拼接/直接造面那一类——
  ``trimesh.util.concatenate``、``extrude_polygon``、``Trimesh(vertices=…)``、
  对 ``.vertices`` / ``.faces`` 赋值、numpy-stl / open3d 造面。
  引擎层产物（``scripts/phase27/build_asset_zoo.py``）也必须过这一级，
  对应 AC-T1-9 ②「源码经 cad_guard 扫描 0 deny」。
- ``RULES_PRIMITIVES``（**仅模型写盘代码**生效，tier="model"）：在上一级
  之上再加 ``trimesh.creation.<any>``。构建脚本自身合法地需要
  ``trimesh.creation.revolve``（STAGE-2 保底）与 ``trimesh.creation.box``
  （``cube_standard``），故它以 tier="engine" 自检——判据不松，只是把
  「原语」与「拼接」两类分开。

为什么 primitives 也要对模型禁：资产库已经把四件水密资产准备好了，
模型被要求的是「选资产 + 定位姿 + 配参数」；放它调
``trimesh.creation.revolve`` 等于把 Task 1 刚移走的那道题又请回来，而且
``concatenate`` 拼壳会产出**假水密**（两个壳各自水密 ⇒ ``is_watertight``
仍为 True，但内部重叠面与自相交给下游凸分解/SPH 埋雷）。
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

TIER_MODEL = "model"          # 模型写盘代码（policy 写入前钩子）
TIER_ENGINE = "engine"        # 引擎层产物自检（AC-T1-9 ②）
TIERS = (TIER_MODEL, TIER_ENGINE)

# 固定拒绝文案（进测试断言，逐字）。
DENY_MESSAGE = (
    "POLICY DENY: 禁止在任务代码中从零构建网格。请改用\n"
    "  from orchestrator.assets.scene_spec import SceneSpec, Body\n"
    '  spec = SceneSpec(engine="genesis", bodies=[Body(asset="vessel/pitcher_standard", '
    "pos=(0,0,1.05))])\n"
    "并把 spec 写入 workdir/scene_spec.json，然后调用 run_probe(\"scene_build\")。")

# 拼接/造面类：引擎层与模型层一律禁。
RULES_ASSEMBLY = (
    "concatenate",
    "extrude_polygon",
    "trimesh_direct_construct",
    "assign_vertices_or_faces",
    "facet_builder_module",
)
# 原语类：只禁模型写盘代码（见模块 docstring 的两级理由）。
RULES_PRIMITIVES = RULES_ASSEMBLY + ("trimesh_creation",)

# 造面类第三方模块的根名（import 别名会先被解析回全名）。
# 注意：**不含** ``trimesh.creation``——原语由 ``trimesh_creation`` 规则
# 在模型层单独拒（L-12 两级），引擎层要靠它做 STAGE-2 回转。
_FACET_MODULE_ROOTS = ("stl", "open3d", "numpy_stl")
_ASSIGN_ATTRS = ("vertices", "faces")


@dataclass(frozen=True)
class Finding:
    rule: str
    lineno: int
    snippet: str


def rules_for(tier: str) -> tuple[str, ...]:
    if tier == TIER_ENGINE:
        return RULES_ASSEMBLY
    if tier == TIER_MODEL:
        return RULES_PRIMITIVES
    raise ValueError(f"未知 tier：{tier!r}（只允许 {TIERS}）")


# ---------------------------------------------------------------------------
# import 别名解析：把 `import trimesh as tm` / `from trimesh import creation`
# 这类写法折算回全名，否则 `tm.util.concatenate(...)` 会漏网。
# ---------------------------------------------------------------------------

def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    root = alias.name.split(".", 1)[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom):
            if node.level:                    # 相对导入与资产无关
                continue
            module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = (f"{module}.{alias.name}" if module
                                  else alias.name)
    return aliases


def _dotted(node: ast.AST, aliases: dict[str, str]) -> "str | None":
    """Attribute/Name 链 → 全点名；别名根被展开。"""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    root = aliases.get(cur.id, cur.id)
    return ".".join([root, *reversed(parts)])


def _call_kwargs(node: ast.Call) -> set[str]:
    out = {kw.arg for kw in node.keywords if kw.arg}
    return out


class _Visitor(ast.NodeVisitor):
    def __init__(self, tier: str):
        self.rules = rules_for(tier)
        self.aliases: dict[str, str] = {}
        self.findings: list[Finding] = []

    # -- 记录 ------------------------------------------------------------
    def _hit(self, rule: str, node: ast.AST, snippet: str) -> None:
        if rule in self.rules:
            self.findings.append(
                Finding(rule=rule, lineno=getattr(node, "lineno", 0),
                        snippet=snippet[:200]))

    # -- 遍历 ------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:      # noqa: N802
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:      # noqa: N802
        self._check_assign_targets(node.targets, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self._check_assign_targets([node.target], node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self._check_assign_targets([node.target], node)
        self.generic_visit(node)

    def _check_assign_targets(self, targets, node) -> None:
        for target in targets:
            for sub in ast.walk(target):
                if (isinstance(sub, ast.Attribute)
                        and sub.attr in _ASSIGN_ATTRS):
                    self._hit("assign_vertices_or_faces", node,
                              f"{sub.attr} = …")

    def visit_Call(self, node: ast.Call) -> None:          # noqa: N802
        name = _dotted(node.func, self.aliases)
        if name:
            snippet = name
            if name.startswith("trimesh.creation."):
                self._hit("trimesh_creation", node, snippet)
            # 直接构造：裸名 ``Trimesh(...)`` 与全名同罪。
            if name.split(".")[-1] == "Trimesh":
                self._hit("trimesh_direct_construct", node, snippet)
            if name.split(".")[-1] == "concatenate":
                self._hit("concatenate", node, snippet)
            if name.split(".")[-1] == "extrude_polygon":
                self._hit("extrude_polygon", node, snippet)
            if any(name == root or name.startswith(root + ".")
                   for root in _FACET_MODULE_ROOTS):
                self._hit("facet_builder_module", node, snippet)
        self.generic_visit(node)


def _strip_comments_and_strings(source: str) -> "str | None":
    """文本通道：去掉注释与字符串字面量（避免 docstring 里「提到了规则」
    就被当成违规），保留代码行与行号。tokenize 失败返 None。"""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    out: list[str] = []
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


# 文本通道的标记（AST 解析失败时才用；只认拼接类，避免误杀）。
_TEXT_MARKERS = (
    (r"\bconcatenate\s*\(", "concatenate"),
    (r"\bextrude_polygon\s*\(", "extrude_polygon"),
    (r"\.\s*(vertices|faces)\s*=(?!=)", "assign_vertices_or_faces"),
    (r"\bTrimesh\s*\(", "trimesh_direct_construct"),
)


def scan_source(source: str, *, tier: str = TIER_MODEL) -> list[Finding]:
    """扫描源码，返回命中列表（空 = 通过）。同参同决、无 IO。"""
    if not isinstance(source, str):
        raise ValueError("scan_source 只接受 str（bytes 请先解码）")
    rules = rules_for(tier)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _scan_text(source, rules)
    visitor = _Visitor(tier)
    visitor.aliases = _import_aliases(tree)
    visitor.visit(tree)
    # 去重：同一行同一规则只报一次。
    seen: set[tuple[str, int]] = set()
    unique: list[Finding] = []
    for f in visitor.findings:
        key = (f.rule, f.lineno)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique


def _scan_text(source: str, rules: tuple[str, ...]) -> list[Finding]:
    stripped = _strip_comments_and_strings(source)
    if stripped is None:
        stripped = source                       # 最后兜底：原样扫
    findings: list[Finding] = []
    for pattern, rule in _TEXT_MARKERS:
        if rule not in rules:
            continue
        if re.search(pattern, stripped):
            findings.append(Finding(rule=rule, lineno=0,
                                    snippet=pattern))
    return findings


def scan_file(path: "str | Path", *, tier: str = TIER_MODEL) -> list[Finding]:
    return scan_source(Path(path).read_text(encoding="utf-8"), tier=tier)


def deny_reason(source: str, *, tier: str = TIER_MODEL) -> "str | None":
    """命中即返回固定拒绝文案（附首个命中位置），未命中返回 None。"""
    findings = scan_source(source, tier=tier)
    if not findings:
        return None
    first = findings[0]
    return (f"{DENY_MESSAGE}\n[命中] {first.rule}"
            + (f" 行 {first.lineno}" if first.lineno else ""))


def cad_guard_allows(source: str, *, tier: str = TIER_MODEL) -> bool:
    return not scan_source(source, tier=tier)
