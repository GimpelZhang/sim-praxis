"""图结构导出（契约 L492⑥）：pygraphviz 可选，失败用内置文本拓扑。

明确不为此安装任何系统包（networkx/pygraphviz 均非 pinned 依赖）：
LangGraph 0.2.28 自带 get_graph().draw_ascii()，作为文本回退；
另存 draw_mermaid() 原文便于无渲染器查看。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_NODE_LABEL = {
    "__start__": "START", "__end__": "END",
    "direction": "direction(Task2)",
    "gate1": "gate1(Task1 阻塞)",
    "agent_run": "agent_run(Task4 接缝)",
    "evaluate": "evaluate(Task5 接缝, No-Fake-Physics)",
    "gate2": "gate2(Task1 阻塞)",
    "persist_learning": "persist_learning(Task6 接缝)",
    "human_failure": "human_failure(FAILURE.json)",
    "done": "done(DONE.json)",
}

_CONDITIONAL = {
    ("gate1",): ("route_after_gate1",
                 {"approve": "agent_run",
                  "reject & gate1_rejects<2": "direction",
                  "reject & gate1_rejects==2 (或空意见)": "human_failure"}),
    ("gate2",): ("route_after_gate2",
                 {"approve": "persist_learning",
                  "reject & gate2_rejects<2": "agent_run",
                  "reject & gate2_rejects==2 (或空意见)": "human_failure"}),
    ("__start__",): ("route_track", {"track1": "direction", "track2": "direction"}),
}


def export_text(compiled: Any) -> str:
    """确定性文本拓扑：节点清单 + 条件分支表 + 全边列表 + draw_ascii。"""
    draw = compiled.get_graph()
    lines = ["SimPraxis LangGraph topology (Task3)",
             "=" * 60, "nodes:", ""]
    for node in draw.nodes.values():
        label = _NODE_LABEL.get(node.id, node.name)
        lines.append(f"  - {node.id}: {label}")
    lines += ["", "conditional branches:", ""]
    for keys, (fn, mapping) in _CONDITIONAL.items():
        lines.append(f"  @ {keys[0]}  {fn}:")
        for cond, target in mapping.items():
            lines.append(f"      {cond} -> {target}")
    lines += ["", "edges (raw, langgraph DrawableGraph):", ""]
    for edge in sorted(draw.edges, key=lambda e: (e.source, e.target)):
        flag = "  [conditional]" if edge.conditional else ""
        lines.append(f"  {edge.source} -> {edge.target}{flag}")
    lines += ["", "draw_ascii():", ""]
    try:
        lines.append(draw.draw_ascii())
    except Exception as exc:  # noqa: BLE001 - 回退不得让证据导出整体失败
        lines.append(f"(draw_ascii 不可用：{type(exc).__name__}: {exc})")
    return "\n".join(lines) + "\n"


def export_svg(compiled: Any, out_path: "str | Path") -> "Path | None":
    """pygraphviz 可用时导 svg；不可用返回 None（调用方落文本回退，不装包）。"""
    try:
        import pygraphviz  # noqa: F401  (仅探测；真正绘制走 langgraph)
    except Exception:
        return None
    try:
        # pygraphviz 直绘（不调 draw_mermaid_png：它需要额外渲染器/网络）
        import pygraphviz as pgv  # type: ignore
        draw = compiled.get_graph()
        g = pgv.AGraph(directed=True)
        for node in draw.nodes.values():
            g.add_node(node.id)
        for edge in draw.edges:
            g.add_edge(edge.source, edge.target)
        g.layout(prog="dot")
        g.draw(str(out_path))
        return Path(out_path)
    except Exception:
        return None
