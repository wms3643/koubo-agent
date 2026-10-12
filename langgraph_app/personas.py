"""四个专业角色的 persona —— **从 `agents/subagents.patch.yml` 加载，不复制一份**。

为什么必须是加载而不是重写：那份 persona 是本项目反复打磨出来的（红线、工具边界、
音画同步的两份时间轴、不遮挡区域…）。在 LangGraph 这版里再抄一遍，两边立刻开始漂移 ——
而漂移的后果是"DSH 版守住的规矩，LangGraph 版没守住"，且**没人会注意到**。

这也让迁移变得可逆：你改那份 yaml，两个编排层同时生效。

映射依据：yaml 里每个子代理用 `toolName` 标识（`delegate_transcript_editor` 等），
这里把它对上 `stages.py` 里的中文角色名。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from . import stages as S

# 仓库根 = 本文件的上一级
REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_FILE = REPO_ROOT / "agents" / "subagents.patch.yml"

# yaml 里的 toolName → 中文角色名（与 stages.OWNER 的取值必须一致）
TOOLNAME_TO_ROLE: dict[str, str] = {
    "delegate_transcript_editor": S.CUT_PLANNER,
    "delegate_subtitle_maker": S.SUBTITLE_MAKER,
    "delegate_overlay_designer": S.OVERLAY_DESIGNER,
    "delegate_qc_inspector": S.QC_INSPECTOR,
}


class PersonaMissing(RuntimeError):
    """角色 persona 取不到 —— **fail-loud**：宁可起不来，也不要拿空 persona 去跑。"""


@lru_cache(maxsize=1)
def load_personas() -> dict[str, str]:
    """返回 `{中文角色名: persona 文本}`。"""
    if not PATCH_FILE.is_file():
        raise PersonaMissing(f"找不到 persona 源文件：{PATCH_FILE}")

    data = yaml.safe_load(PATCH_FILE.read_text(encoding="utf-8")) or []
    out: dict[str, str] = {}
    for entry in data:
        for item in (entry or {}).get("insert", []) or []:
            config = (item or {}).get("config") or {}
            tool_name = config.get("toolName")
            persona = config.get("persona")
            role = TOOLNAME_TO_ROLE.get(tool_name or "")
            if role and persona:
                out[role] = str(persona).strip()

    missing = set(TOOLNAME_TO_ROLE.values()) - set(out)
    if missing:
        raise PersonaMissing(
            f"这些角色的 persona 没有从 {PATCH_FILE.name} 里读到：{sorted(missing)}。"
            "（检查 yaml 的 toolName 是否与 TOOLNAME_TO_ROLE 对得上）"
        )
    return out


def persona_for_node(node: str) -> str:
    """取某个节点负责人（角色）的 persona。"""
    role = S.owner_of(node)
    persona = load_personas().get(role)
    if not persona:
        raise PersonaMissing(f"角色 {role}（节点 {node}）没有 persona")
    return persona


# ---------------------------------------------------------------------------
#  编排层追加的"接口说明"
# ---------------------------------------------------------------------------
# ⚠️ persona 原本是写给 DSH 子代理的（它自己手里就有 kb_* 工具，自己去调）。
#    在 LangGraph 这版里，调用工具的活儿由**编排层**代劳，模型只负责**出判断**。
#    这层差异必须显式说明，否则模型会以为"我该去调 kb_cut"，然后产出一段
#    "我要调用 kb_cut(…)" 的文字而不是决策 JSON。
INTERFACE_PREAMBLE = """\
【本版运行方式（与 DSH 版的唯一差别，务必看清）】

你不是在 DSH 里运行，**你的工具调用由编排层代劳**。具体说：

  · 上面 persona 里提到的 `kb_*` 工具，**你无法直接调用**，也**不要**在回复里写
    "我调用 kb_xx(...)" 这类话 —— 那是编排层的活。
  · 你只需**产出一个 JSON 对象**作为回复（不要围栏、不要解释、不要多余文字），
    编排层会照它写文件、调内核、并把内核的失败原样回喂给你。
  · 文件是唯一的交接面：你看不到别的 agent 的对话，只能看文件；
    你写的东西也必须落到文件里（由编排层代笔）。

如果你认为"这件事做不了"（依据不足 / 与红线冲突），就按下面的 JSON 契约里的
`abort` 字段说明理由 —— 不要硬编一个看起来合理的答案出来。
"""


def system_prompt_for_node(node: str, extra: str = "") -> str:
    """拼出完整的 system prompt：persona + 接口说明 + 本次任务要求。"""
    parts = [persona_for_node(node), INTERFACE_PREAMBLE]
    if extra.strip():
        parts.append(extra.strip())
    return "\n\n".join(parts)
