"""工具归属：**每个工具恰好属于一个 agent** —— 把这条约定变成可执行的断言。

## 为什么值得单独一个测试

"零重叠"是个**口头约定**，代码里没有任何强制：`build_tools(ctx, only=...)` 只做过滤，
在多个 `spec.tools` 里写同一个工具名**完全合法**（DSH 版就这么干过：
`kb_status` 挂 4 个子代理、`kb_assemble` 挂 2 个）。

而它一旦被破坏，代价是**静默**的：
并发闸门判冲突靠各 `spec` 的 `writes` **声明**，工具一旦共享，
"它到底写哪些产物"就得在**多处保持手工一致** —— 漏一处就退化成
"可能并发写同一个文件"，而且**不报错**。

所以这里守四件事：
1. 没有工具漏配（有工具但没人能调 ⇒ 那件事永远做不了）；
2. 白名单里没有不存在的工具（拼错了别等到运行时才炸）；
3. 没有未登记的重叠；
4. 主管手里没有业务工具（这是"派活"能成立的**机制**保证）。

要放开某个工具给多个 agent ⇒ 往 `ALLOWED_SHARED` 加一行。
**这一行就是"这是有意为之"的书面记录**，也是不让它悄悄发生的那道门槛。
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from langgraph_app.agent.director import DIRECTOR_TOOLS  # noqa: E402
from langgraph_app.agent.subagents import SUBAGENTS  # noqa: E402
from langgraph_app.agent.tools import ToolContext, build_tools  # noqa: E402
from langgraph_app.config import AppConfig  # noqa: E402
from langgraph_app.kernel import KernelClient  # noqa: E402

# ⛔ 显式例外表：默认空。往这里加一行 = 「这个工具确实该被多人共用」。
#
# 加进来的**判据只有一条**：共享它，会不会让「它到底写哪些产物」这件事
# 需要在多处手工保持一致？
#   · 会  ⇒ 不该共享（漏一处就退化成"可能并发写同一文件"，且不报错）；
#   · 不会 ⇒ 可以共享。
# `skills/` 那两个工具是**只读**（读知识包目录，不写任何项目产物）⇒ 符合第二条。
ALLOWED_SHARED: dict[str, set[str]] = {
    # 四个子代理都该能查"我这次要做的活有没有对应规范"。
    "list_skills": {"剪裁策划", "字幕制作", "特效设计", "成片质检"},
    "load_skill": {"剪裁策划", "字幕制作", "特效设计", "成片质检"},
}


def _owners() -> dict[str, list[str]]:
    """工具名 → 持有它的 agent（按 `spec.tools` 与 `DIRECTOR_TOOLS` 读，不执行任何东西）。"""
    owners: dict[str, list[str]] = collections.defaultdict(list)
    for spec in SUBAGENTS:
        for n in spec.tools:
            owners[n].append(spec.title)
    for n in DIRECTOR_TOOLS:
        owners[n].append("主管")
    return owners


def _available() -> set[str]:
    """内核真能造出来的工具全集。"""
    cfg = AppConfig()
    ctx = ToolContext(project=None, config=cfg, client=KernelClient(cfg))
    return {t.name for t in build_tools(ctx)}


def test_no_tool_is_orphan():
    """每个工具都得有人能调 —— 有工具但没人持有 ⇒ 那件事谁也做不了。"""
    missing = sorted(_available() - set(_owners()))
    assert not missing, f"这些工具没人持有（漏配）：{missing}"


def test_no_phantom_tool():
    """白名单里不能出现不存在的工具。

    （`build_tools(only=...)` 遇到拼错的名字确实会 `KeyError`，但那是**运行时**；
      这里把它提前成测试失败，省得等到真派活才发现。）
    """
    phantom = sorted(set(_owners()) - _available())
    assert not phantom, f"白名单里有内核根本没有的工具：{phantom}"


def test_every_tool_has_exactly_one_owner():
    """**零重叠** —— 除显式登记的例外，每个工具恰好一个持有者。"""
    shared = {n: w for n, w in _owners().items() if len(w) > 1}
    undeclared = {n: w for n, w in shared.items() if n not in ALLOWED_SHARED}
    assert not undeclared, (
        f"这些工具被多个 agent 持有，但没在 ALLOWED_SHARED 里登记：{undeclared}\n"
        "⛔ 共享工具会让『它写哪些产物』的声明必须在多处手工保持一致 —— 漏一处就静默失效。"
        "确实要共享，请显式登记（那一行就是『这是有意为之』的记录）。"
    )


def test_allowed_shared_matches_reality():
    """登记的共享范围必须**恰好等于**实际持有者。

    比"只检查不再共享"更严：**多了一个持有者也要显式改这里** ——
    否则新加的 agent 悄悄共享了某个工具，没人会注意到。
    （改子代理名字时这里会失败，正好提醒你：这个共享是有记录的、要一起改。）
    """
    owners = _owners()
    for name, allowed in ALLOWED_SHARED.items():
        actual = set(owners.get(name, []))
        assert actual == allowed, (
            f"`{name}` 的共享登记与实际不符：\n"
            f"  登记 = {sorted(allowed)}\n"
            f"  实际 = {sorted(actual)}\n"
            "改动共享范围请同步更新 ALLOWED_SHARED（那份记录本身就是这道门槛的意义）。"
        )


def test_director_holds_no_business_tool():
    """主管不得直接持有任何"专业活"工具。

    这是"派活"能成立的**机制**保证：不是靠提示词请求它别自己干，
    而是它**根本没有那个工具**。⚠️ 这条不参与 `ALLOWED_SHARED` ——
    主管一旦能直接干专业活，"按需派活"就退化成"可选"了。
    """
    business: set[str] = set()
    for spec in SUBAGENTS:
        business |= set(spec.tools)
    overlap = sorted(DIRECTOR_TOOLS & business)
    assert not overlap, f"主管手里出现了子代理的业务工具：{overlap}"


def test_no_empty_subagent():
    """空白名单 = 派了活什么也干不了（多半是改配置时漏掉了）。"""
    empty = [s.title for s in SUBAGENTS if not s.tools]
    assert not empty, f"这些子代理没有任何工具：{empty}"
