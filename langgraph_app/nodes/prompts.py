"""提示词的可复用片段 —— 单独放一处，避免各节点各写一份（那种散落必然漂移）。

目前只有两块：

    · `feedback_block()` —— 把**上一轮失败证据**背给模型（回退能工作的前提，见下）
    · `evidence_block()` —— 统一"证据从哪来、截断了要不要说"的规矩
"""

from __future__ import annotations

import json

from ..state import PipelineState, feedback_for

# 证据表最多给模型看多少条；超出必须**显式说明**（静默截断＝让模型在残缺证据上做判断）
MAX_EVIDENCE_ROWS = 1200


def feedback_block(state: PipelineState, node: str) -> str:
    """上一轮失败反馈。没有则返回空串。

    ⚠️ 与 `state.feedback_for()` 的分工：那边只负责"取证据"，这边负责"怎么讲"。
    讲法很重要 —— 必须让模型明白**这次不是重新做，是针对上次的失败改**，
    否则它会把同一份答案再输出一遍（提示词没变、温度没变，结果自然也没变）。
    """
    fb = feedback_for(state, node)
    if not fb:
        return ""
    lines = [
        "",
        "=" * 62,
        f"【上一轮失败反馈 · 这是你第 {int(fb.get('attempt') or 0) + 1} 次尝试】",
        f"失败的阶段：{fb.get('failed_at')}",
        f"cause：{'、'.join(fb.get('causes') or []) or '（未给出）'}",
        f"编排层的判读：{fb.get('reason')}",
    ]
    if fb.get("error"):
        lines.append(f"内核原文：{fb['error']}")
    if fb.get("hint"):
        lines.append(f"内核给的提示：{fb['hint']}")
    if fb.get("detail"):
        lines.append(f"证据：{json.dumps(fb['detail'], ensure_ascii=False)[:1800]}")
    lines += [
        "",
        "⛔ 请**针对上面这条失败**改，不要把同一份答案再输出一遍 ——",
        "   提示词与证据都没变，所以原样重答等于没改，会直接把这次回退浪费掉。",
        "=" * 62,
    ]
    return "\n".join(lines)


def evidence_block(title: str, rows: list[str], *, limit: int = MAX_EVIDENCE_ROWS) -> str:
    """拼一段证据表，并在**截断时明说**。

    ⚠️ 静默截断比不截断更危险：模型会以为"这些就是全部"，于是对着一小部分证据
    做整体判断（例如只看到前 1200 段就断定"后面没有重复块"）。
    宁可告诉它"只给了前 N 条"，让它主动要求更多证据或改变策略。
    """
    shown = rows[:limit]
    out = [f"{title}（共 {len(rows)} 条" + (f"，这里给出前 {limit} 条）" if len(rows) > limit else "）")]
    if len(rows) > limit:
        out.append(f"⚠️ 已截断：省略了后 {len(rows) - limit} 条 —— 不要据此断言"
                   "「后面没有类似情况」；若你怀疑漏看了，就在 notes 里说明。")
    out.extend(shown)
    return "\n".join(out)
