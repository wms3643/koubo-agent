"""节点集合装配。

`build_nodes()` 是**唯一**把节点凑齐的地方。两个能力开关：

    · `llm=False` —— 不装载判断节点。桩测试用它可以只跑确定性链路：
      **不需要密钥、不花一分钱、也不会因为模型抽风而 flaky**。
      缺的节点由调用方用 `overrides` 补（测试里的假判断节点）。
    · `overrides` —— 按节点名覆盖实现（测试注入桩、或换掉某个阶段）。

刻意不做"缺节点就静默跳过"：图必须在**装配时**就自洽（`graph._assert_complete`），
否则跑到一半才发现某个阶段没人实现，那时进度已经推进了一半。
"""

from __future__ import annotations

from ..config import AppConfig
from ..kernel import KernelClient
from .base import NodeFn
from .control import make_approval, make_deliver, make_halt, make_override, make_probe
from .kernel_nodes import make_kernel_nodes
from .llm_nodes import make_llm_nodes


def build_nodes(
    client: KernelClient,
    config: AppConfig,
    *,
    llm: bool = True,
    overrides: dict[str, NodeFn] | None = None,
) -> dict[str, NodeFn]:
    from .. import stages as S

    nodes: dict[str, NodeFn] = {}
    nodes.update(make_kernel_nodes(client, config))
    if llm:
        nodes.update(make_llm_nodes(client, config))
    nodes[S.PROBE] = make_probe(client, config)
    nodes[S.OVERRIDE] = make_override(client, config)
    nodes[S.APPROVAL] = make_approval(config)
    nodes[S.DELIVER] = make_deliver(config)
    nodes[S.HALT] = make_halt(config)
    nodes.update(overrides or {})
    return nodes
