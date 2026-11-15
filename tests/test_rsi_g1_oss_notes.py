"""RSI-G1 资料清单：静态引用、无凭据、无 SOTA 断言、非运行依赖。"""
from __future__ import annotations

import ast
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "rsi_g1_oss_references.md"


def test_doc_exists_and_marks_unverified():
    text = DOC.read_text(encoding="utf-8")
    assert "OpenEvolve" in text and "UNVERIFIED" in text
    for name in ("Darwin", "AI Scientist", "GEPA"):
        assert name in text
    assert "非 SOTA 证据" in text or "不是 SOTA" in text or "不支持" in text


def test_no_credentials_or_tokens_in_doc():
    text = DOC.read_text(encoding="utf-8")
    for marker in ("ghp_", "ANTHROPIC_AUTH", "Bearer ", "token=",
                   "sk-", "password"):
        assert marker not in text, marker


def test_no_runtime_module_imports_the_doc_or_projects():
    root = Path(__file__).resolve().parents[1] / "orchestrator" / "rsi_r1"
    for path in root.glob("g1_*.py"):
        src = path.read_text(encoding="utf-8")
        assert "oss_references" not in src, path.name
        assert "import dgm" not in src and "gepa" not in src.lower(), path.name
    tree = ast.parse((root / "g1_cli.py").read_text(encoding="utf-8"))
    names: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not [n for n in names if "requests" in n or "httpx" in n]
