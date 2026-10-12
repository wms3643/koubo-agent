"""图装配 —— 把节点连成一条**可断点续跑、可在失败处回退**的流水线。

装配方式刻意极简（只有一条 START 边），因为**路由全在节点里**：
每个节点返回 `Command(goto=..., update=...)`，去哪儿由 `routing.py` 按矩阵裁决。
这样做的两个理由：

    · **只有一份流向定义**。用条件边也能做到，但那样"正常后继"（`stages.FOLLOWING`）
      与"回退目标"（`routing.MATRIX`）会分散在图的连边和矩阵两处，迟早对不上。
      集中到 `routing.advance()` 之后，"谁去哪"只有一个入口，测试也只测一处。
    · **`Command` 天然支持扇出**（`goto=[a, b]`），将来把"字幕线 ∥ 特效线"打开时
      不必重构图，只需在矩阵/后继表里改一处。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from . import stages as S
from .config import AppConfig
from .kernel import KernelClient
from .nodes import NodeFn, build_nodes
from .state import PipelineState


def build_graph(
    config: AppConfig,
    *,
    client: KernelClient | None = None,
    nodes: dict[str, NodeFn] | None = None,
    checkpointer=None,
    llm: bool = True,
    node_overrides: dict[str, NodeFn] | None = None,
):
    """编译图。`checkpointer` 是**断点续跑与 interrupt 的必要条件**（不给就只能跑一次）。"""
    client = client or KernelClient(config)
    if nodes is None:
        nodes = build_nodes(client, config, llm=llm, overrides=node_overrides)
    _assert_complete(nodes)

    g = StateGraph(PipelineState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, S.PROBE)
    # ⚠️ DELIVER / HALT **不加**静态边：它们自己返回 `Command(goto=END)`。
    #    同时存在静态边与 Command 时后者优先，留着静态边只会让人误以为它有用。
    return g.compile(checkpointer=checkpointer)


def _assert_complete(nodes: dict[str, NodeFn]) -> None:
    """图必须在**装配时**自洽 —— 缺节点要在这里炸，而不是跑到一半才发现没人干活。"""
    missing = sorted(S.known_nodes() - set(nodes))
    if missing:
        raise RuntimeError(
            f"图不完整，缺少这些节点的实现：{missing}。"
            "（用 `llm=False` 时需要由 `overrides` 补齐判断节点）"
        )


def open_checkpoint_db(config: AppConfig) -> sqlite3.Connection:
    """打开 SQLite 检查点库，返回**连接**。

    ⚠️ 为什么返回连接而不是 Saver：`SqliteSaver.from_conn_string()` 是上下文管理器，
    退出即关库。若在 `build_graph()` 内部用它，函数一返回连接就没了 ——
    表现是"第一次跑正常、恢复时读不到检查点"，而且**不报错**。
    所以连接的生命周期由调用方持有（`run.py` 用 try/finally 关掉）。
    """
    path = Path(config.checkpoint_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path), check_same_thread=False)


def make_checkpointer(conn: sqlite3.Connection):
    """把连接包成 LangGraph 的 checkpointer（`SqliteSaver`）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(conn)
