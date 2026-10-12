"""主从多 agent 的**从**：4 个专业子代理。

## 为什么要拆（不是"分工好看"）

单个 agent 拿 23 个工具时，真正的代价不是 token 开销，而是**上下文污染**。
实测：`list_segments` 一次返回 2947 字符、`inspect_project` 552 字符，
而**每一步的原始产物都永久留在这同一条对话里**。跑一次"加字幕"要六七轮工具，
跑到后面主管面前堆的是前面每一步的流水账。

子代理的价值就在这里 —— **它自己吃掉那一大坨原始产物，只把结论交回主管。**

## 三条硬规矩

1. **工具白名单**：每个子代理只看得见自己那一份（字幕制作根本看不见花钱的 `generate_bgm`）。
   这是第一道闸；真正的付费闸门仍是服务端授权位 —— 两道都要有。
2. **只交结论，且由代码兜底**：提示词里写了"别贴原始 JSON"，但提示词是**建议**。
   真正的保证是这里的 `_clip()` —— 子代理就算把 20KB 倒出来，交到主管手里也只有 2200 字符。
3. **共享同一个 `ToolContext`**：项目中途被换掉时，子代理跟着换
   （工具里的 `_LiveProject` 按调用时解析），不需要重建。

## 主从怎么连

每个子代理 = 一个 `create_react_agent` **包成一个工具**挂给主管。
所以主管的工具箱里出现的是 `delegate_subtitle_maker` 这样的名字，
它调一次，等于"派了一个人去做这件事，等他回来汇报"。

⚠️ 子代理**不挂 checkpointer** —— 它们是**单次工人**，不是有记忆的对话者。
每次调用都是干净的：给任务、干完、交结论、结束。
"""

from __future__ import annotations

import contextlib
import itertools
import json
import threading
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

from ..llm import build_chat
from .tools import ToolContext, build_tools

# 子代理自己的 ReAct 循环上限。比主管紧得多 —— 它是**单次工人**，
# 一个工人要是二十几步还没干完，多半是卡住了，不如早点把失败交回去让主管决定。
SUB_RECURSION_LIMIT = 20

# 交回主管的结论上限（字符）。这是"上下文隔离"的**实际执行点**：
# 提示词只是建议，这个数字才是保证。
SUB_REPORT_LIMIT = 2200

# 每个中间步骤给界面看的返回上限（存进事件流水，所以也要设限）。
SUB_STEP_LIMIT = 1200


def text_of(msg: Any) -> str:
    """把消息内容压成纯文本（内容可能是 str，也可能是 block 列表）。"""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                parts.append(str(b.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文 {len(text)} 字符）"


def _verdict(content: str) -> tuple[bool | None, str]:
    """从工具返回的 JSON 里挑出 ok / cause —— 界面靠它上色，不靠猜。"""
    try:
        d = json.loads(content)
        if isinstance(d, dict):
            return d.get("ok"), str(d.get("cause") or "")
    except Exception:  # noqa: BLE001 —— 不是 JSON 就当"无从判断"
        pass
    return None, ""


# ============================================================================
#  四个子代理的编制
# ============================================================================
@dataclass(frozen=True)
class SubAgent:
    name: str                 # 工具名（主管看到的就是这个）
    title: str                # 中文名（界面上显示这个）
    brief: str                # 给主管看的工具描述
    tools: tuple[str, ...]    # ⛔ 工具白名单
    prompt: str               # 它自己的系统提示词
    # 「资源声明」—— 判断"能不能和别的子代理同时跑"的唯一依据。
    # 普通路径 = 具体文件；以 `/` 结尾 = 整个目录。见 `_resource_gate`。
    # ⚠️ 声明写错 = 该挡的没挡（静默出错）或该并行的被串起来（白等）。
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    # 「硬前置」—— 不满足就**当场拒绝，连子代理都不启动**。
    # 为什么要有：`reads` 是"会碰什么"（并发用），`requires` 是"没有就干不了"（门禁）。
    # 字幕与动效都建立在**剪裁产出的时间轴**上，所以它们的前置是剪裁那一阶段的产物。
    # 没有这道门禁，子代理会白跑一轮模型调用、再回一句"缺前置" —— 那一步纯属浪费，
    # 而且"缺什么"这件事本来是**确定性可判**的，不该交给模型去发现。
    requires: tuple[str, ...] = ()
    # 被前置挡住时，给主管的下一步建议（主管照着它去补前置）。
    blocked_hint: str = ""


PLANNING_NOTE = """\

## 交回什么（这条和你的手艺一样重要）

你只交**结论**。原始 JSON、整张句子表、工具的完整返回 —— **不要贴回来**。
主管那边只需要知道"成了没有、结果是什么、有什么后果"：

· 做了什么（转写 / 建时间轴 / 删了哪几段）
· 关键数字（段数、剪后时长、字幕条数……）
· 有后果就照实说（哪些下游产物作废了、必须重跑什么）
· 没做成 ⇒ 直说卡在哪一步、缺什么前置

三四行以内。写多了不但没用，还会把主管的上下文挤满 —— 那正是设你出来的原因。
"""


# 每个子代理**共同**挂的两个工具：知识包（只读）。
# 为什么它可以共享（而别的工具不行）：它们**只读 `skills/` 目录、不写任何项目产物**
# ⇒ 不存在"共享工具会让 writes 声明要在多处手工一致"那个风险。
# （这条也有测试守着：`tests/test_agent_roster.py` 要求共享工具显式登记。）
SKILL_TOOLS: tuple[str, ...] = ("list_skills", "load_skill")

# 拼到**每个**子代理提示词末尾（与 `PLANNING_NOTE` 同一手法：公共纪律只写一处）。
SKILL_NOTE = """

## 你手上有「知识包」（skill）

- `list_skills` —— 列出本项目可用的参考资料（规范、阈值、规则全集这类），**只有目录**；
- `load_skill(name)` —— 读某个知识包的**全文**（名字照抄，别自己拼）。

⚠️ **动手前先 `list_skills` 扫一眼**：排版 / 渲染契约这类硬规矩，写错了往往要到
渲染或质检才暴露，而那时已经烧掉几分钟。踩过一次的坑，多半已经写成知识包了。

⚠️ 但**只读相关的** —— 不相关的读进来只占你自己的上下文。
（你的上下文是干净的：进来干活、交结论、结束。）"""

# 逐个写的编制表。⚠️ 名字带下划线：对外的 `SUBAGENTS` 在下面统一拼上 SKILL_TOOLS ——
#   手工在 4 个 spec 里各加一遍，迟早漏一个。
_BASE: tuple[SubAgent, ...] = (
    SubAgent(
        name="delegate_transcript_editor",
        title="剪裁策划",
        brief=(
            "剪裁策划：把**已经转写好的逐字稿**变成**确定下来的时间轴** —— "
            "看清每句话、判断该删哪些段、执行剪切。"
            "给它一句话的任务（说清用户要什么），它自己决定调哪些工具。"
            "它**不碰**字幕、动效、BGM、质检。"
            "凡是会产生大堆中间产物的活都派给它 —— 它只把结论交回你。"
            "⚠️ 转写不归它（那是你的 `transcribe`）——派它之前逐字稿要已经在了。"
        ),
        tools=("list_segments", "decide_cuts",
               "set_cut_plan", "apply_cut", "establish_timeline"),
        reads=("edit/transcript.json", "input/raw.mp4"),
        writes=("edit/plan_context.json",
                "edit/edit_decisions.json", "edit/cut_blocks.json",
                "edit/timeline_map.json", "renders/edited_final.mp4"),
        # ⭐ 前置是**逐字稿**而不是源片：剪辑判断建立在"看清了每句话"之上。
        #    转写已经上移到主管手里（它是三个下游的公共素材，见 `_ensure_timeline` 的说明），
        #    所以"源片在不在"不再由它兜 —— 逐字稿在 ⇒ 源片一定在（转写需要源片）。
        requires=("edit/transcript.json",),
        blocked_hint=("剪辑判断要先有逐字稿。逐字稿是你的 `transcribe` 的产物 —— "
                      "先转写，再派「剪裁策划」。"),
        prompt="""\
你是「剪裁策划」。主管把一个和**剪辑**有关的任务交给你，你做完，交回结论。

## 你管什么

把**已有的逐字稿**变成"确定下来的时间轴"：看清每一句、判断该删哪些段、执行剪切。
你手上只有这几个工具，**转写、字幕、动效、BGM、质检都不是你的活**。
（逐字稿是主管转好的，进来时就在 `edit/transcript.json`。）

## 铁律：不要替用户做他没要求的删减

先分清是哪种：

· 用户**没要求删减**（"加字幕"这类只是要个时间轴当地基）⇒ `establish_timeline`，**零删除**。
· 用户**明确要删减**（"剪掉废话""去掉开头那几秒"）⇒ 才动 `decide_cuts` / `set_cut_plan` + `apply_cut`。

⚠️ 分不清就问，或者先按"零删除"做 —— 多删的内容用户**拿不回来**。
（真事：用户只说"加字幕"，却因为走了 `decide_cuts`，被自行删掉了 6 段。这是本项目最贵的一次越界。）

## 选工具的判据

· 用户**点名**了哪几句（"第 3 句""开头两段"）⇒ 先 `list_segments` 看清编号，再用 `set_cut_plan`（确定性）。
  ⚠️ 用户说的"第 N 句"是 **1 起**的人话序号，而 `drop_idx` 是 **0 起** ⇒ 要**减 1**。
· 用户只给了**模糊标准**（"把废话剪掉"）⇒ `decide_cuts`，把**用户原话原样**放进 `instructions`。
· 删"幻觉嫌疑段"会被内核拒绝（`hallucination_cut_forbidden`）—— 那是刻意设的红线，不要绕。

## 一个容易多干的事

· **动效不需要你**（它只要时间戳，`design_overlay` 自己会补时间轴）。
  别为了"给动效当地基"去跑 `establish_timeline` —— 那只会在盘上多产一份视频副本。
  字幕才需要（要烧录就得有底片）。
· 已经有时间轴了（`edit/timeline_map.json` 在）就别再建一遍 —— 会白产一份视频。
""" + PLANNING_NOTE,
    ),

    SubAgent(
        name="delegate_subtitle_maker",
        title="字幕制作",
        brief=(
            "字幕制作：字幕的**文本、时间轴、烧录与排版复核**。"
            "给它一句话的任务，它自己决定调哪些工具（含「先烧测试片看一眼再烧全片」这套流程）。"
            "它**不碰**剪辑、动效、BGM。"
            "烧全片是几十秒的真实计算，交它做省得你盯着。"
        ),
        tools=("make_subtitles", "fix_subtitle_text", "set_subtitle_text",
               "burn_subtitles", "review_test_burn"),
        reads=("edit/transcript.json", "edit/cut_blocks.json",
               "edit/timeline_map.json", "renders/edited_final.mp4"),
        writes=("edit/subtitles/", "edit/subtitle_fixes.json",
                "renders/subtitle_test_10s.mp4", "renders/subtitled_full.mp4",
                "edit/tmpimg/"),
        # 字幕每一行的时间都是按**保留块**从原片时间轴重映射出来的（`stage3_srt` 直接读它），
        # 所以"剪辑没定下来 ⇒ 字幕做不了"是**硬前置**，不是建议。
        requires=("edit/cut_blocks.json",),
        blocked_hint=("字幕要按「保留块」重映射时间轴，所以必须先有剪辑决策。"
                      "先派「剪裁策划」（delegate_transcript_editor）："
                      "用户没要求删减就用零删除建时间轴，要求了才真删。"),
        prompt="""\
你是「字幕制作」。主管把一个和**字幕**有关的任务交给你，你做完，交回结论。

## 你管什么

字幕的文本、时间轴、烧录与排版复核。
**剪辑 / 动效 / BGM / 质检不是你的活** —— 需要它们的时候，交回主管去派别人，不要自己动手。

## 干活时的判据

· **生成字幕要求剪辑时间轴已经存在**（`edit/cut_blocks.json`）。
  缺它 ⇒ 直接交回"需要先确定剪辑时间轴"，**不要自己去剪**（那是剪裁策划的活）。
· **烧录必须先测试片**：`burn_subtitles(mode=test)` 烧前 10 秒 → `review_test_burn` 看图复核
  → **通过了**才 `burn_subtitles(mode=full)` 烧全片。
  ⛔ 不要跳过测试片直接烧全片：一次全片几十秒，排版错了就是白烧一遍。
· 复核没过 ⇒ 改了再烧测试片，**别硬着头皮烧全片**。
· 改某一句的文字用 `set_subtitle_text`（0 起下标）；整片纠错用 `fix_subtitle_text`。
  人工改过的条目不会被 `fix_subtitle_text` 覆盖。
· 复核时看清画面里是谁 —— "人工智能"这类词容易被误判，别照抄模型的第一反应。
""" + PLANNING_NOTE,
    ),

    SubAgent(
        name="delegate_overlay_designer",
        title="特效设计",
        brief=(
            "特效设计：**动效工程的设计、渲染，以及叠到成片上**。"
            "给它一句话的任务，它自己决定调哪些工具。它**不碰**字幕、剪辑、BGM。"
            "⚠️ 渲染动效是分钟级、产物上百 MB，所以「要不要渲」这种判断交给你、执行交给它。"
        ),
        tools=("design_overlay", "render_overlay", "apply_overlay"),
        # ⭐ 刻意**不**声明读 `renders/assembled.mp4` —— 那是 `apply_overlay` 才需要的
        #    （叠加要垫在混音成片上），而 `design_overlay` / `render_overlay`
        #    这两步**根本不读任何视频**（`kb_overlay` 的输入只有 `overlays/index.html`）。
        #    ⛔ 声明了就会和"烧字幕"串起来 —— 白白多等两分钟。这是真实教训。
        #    `apply_overlay` 的输入新鲜度改由**运行时前置检查**兜底（见 tools.apply_overlay），
        #    而且宁可**明确报错**也不静默用旧底片。
        reads=("edit/transcript.json", "edit/timeline_map.json", "overlays/index.html"),
        writes=("overlays/", "edit/assemble.json", "renders/final.mp4"),
        # ⭐ 前置是**逐字稿**，不是时间轴 —— 动效只需要**时间戳**：
        #    `design_overlay` 把秒数写进 GSAP，`render_overlay` 只读 `overlays/index.html`。
        #    没剪过片时逐字稿的原始秒数**就是**成片秒数（`edit_start ≡ source_start`），
        #    所以「加动效」**不需要先经过剪裁**。
        #    ⚠️ 兜底路径（缺时间轴时）会额外写 `edit/timeline_map.json`，
        #    但那**只在未剪片时发生**；那时剪辑/字幕都被各自的前置挡着，没有并发的读者。
        #    所以这里的 `reads` 声明是安全的 —— 声明成 write 反而会误伤
        #    「已剪片时 特效设计 ∥ 字幕制作」这条合法并行。
        requires=("edit/transcript.json",),
        blocked_hint=("动效每个时间点都取自**逐字稿的时间戳**，所以只要先 `transcribe` 就行 —— "
                      "**不需要先剪裁**（`design_overlay` 会自己把时间轴补到位）。"),
        prompt="""\
你是「特效设计」。主管把一个和**动效**有关的任务交给你，你做完，交回结论。

## 你管什么

动效工程的设计、渲染，以及叠到成片上。
**字幕 / 剪辑 / BGM / 质检不是你的活**。

## 干活时的判据

· **顺序固定**：`design_overlay`（写工程）→ `render_overlay`（渲成带 alpha 的透明层 .mov）
  → `apply_overlay`（叠到成片，产出 `renders/final.mp4`）。
· 动效是**透明叠加层**，不是直接改视频。
· 时间轴取自 `edit/timeline_map.json`：**剪过的项目**用它（那份才有正确坐标）；
  **没剪过的**，`design_overlay` 会按「零删除」自动补一份（不产视频、不动画面）。
  ⇒ **你不需要先让谁去剪**，更不要自己剪。
· 渲染是**分钟级、产物上百 MB**。任务只要"看看效果"就渲一层，**别反复渲**。
· ⚠️ `design_overlay` 会**重写** `overlays/index.html` —— "手改工程再重跑"是保不住改动的，
  别走这条路。
· 渲染失败是有具体原因的（`missing_hyperframes` / `hyperframes_lint_failed`）——
  **照实说**，不要说"动效已完成"。
""" + PLANNING_NOTE,
    ),

    SubAgent(
        name="delegate_qc_inspector",
        title="成片质检",
        brief=(
            "成片质检：回答「到底行不行」。跑数字断言 + 抓帧取证（`run_qc`），"
            "也能读中间产物看内容（`read_artifact`）。"
            "⚠️ 它**不改任何产物** —— 它的产物是**结论**。"
            "用户问「这版能用吗」「哪里有问题」就派它。"
        ),
        tools=("run_qc", "read_artifact"),
        # 质检**读得很广**（要读产物才能判），所以声明整个 `renders/` 与 `edit/`：
        # 任何正在写产物的子代理都会和它冲突 ⇒ 它永远在"没有写手"的时候才跑。
        # 这正是想要的 —— 读一个写了一半的文件，得出的结论是废的。
        reads=("renders/", "edit/"),
        writes=("edit/qc_report.json", "edit/tmpimg/"),
        prompt="""\
你是「成片质检」。主管把"**到底行不行**"这个问题交给你。

## 你管什么

只做两件事：跑数字断言与抓帧取证（`run_qc`），读中间产物看内容（`read_artifact`）。
⛔ **你不改任何产物。** 你的产物是**结论**，不是文件。

## 怎么看

· `run_qc` 的 `target` 要选对（选错会有**假报警**）：`final` 看成片、`test` 看测试片。
· 报告 `ok=false` 时，逐个看 `causes`，挑**最可能的那一条**讲清楚，
  ⛔ 别把十几条原因全抄回来 —— 那等于没给结论。
· 抓帧图是证据：字幕位置、可读性、有没有挡脸，都从**图上**说，不靠推测。
· 缺产物 / 读不到文件 ⇒ 直说"还没有这个产物"，**不要凭想象下结论**。
  没有证据就说"通过"，比报错危险得多（本项目吃过这个亏）。
""" + PLANNING_NOTE,
    ),
)


# ============================================================================
#  资源闸门：按**产物**判冲突，而不是"一个项目一把大锁"
# ============================================================================
# 为什么不能用"项目级大锁"：那把锁会把**本该并行**的活也串起来。
# 实测教训（2026-10-07）：烧字幕与渲染动效**互不依赖** ——
#   `burn_subtitles`  : edited_final.mp4 → subtitled_full.mp4
#   `render_overlay`  : overlays/index.html → overlays/renders/*.mov   （不读任何视频）
# 两者只在最后 `apply_overlay` 汇合。项目级大锁会把它们排成一队，白白多等两分钟。
#
# 判据：两个子代理**允许同时跑**，当且仅当它们的（读 ∪ 写）互不相交 ——
#       写写冲突、写读冲突、读写冲突都要挡；读读不挡。
#
# 资源写法：普通路径 = 具体文件；**以 `/` 结尾 = 整个目录**。
# 冲突判定用前缀匹配，所以 `edit/` 会盖住 `edit/transcript.json`，
# `overlays/` 会盖住 `overlays/renders/`。
_GATE = threading.Condition()
_HELD: list[tuple[str, set[str], set[str]]] = []      # (owner, reads, writes)
_SEQ = itertools.count(1)


def _touch(a: str, b: str) -> bool:
    """两个资源是否可能互相影响。"""
    if a == b:
        return True
    if a.endswith("/") and b.startswith(a):
        return True
    if b.endswith("/") and a.startswith(b):
        return True
    return False


def _blocked(reads: set[str], writes: set[str]) -> bool:
    """和**已经在跑**的那些比：写↔写、写↔读、读↔写 任一相交就冲突（**读↔读不算**）。"""
    for _owner, h_reads, h_writes in _HELD:
        for a in writes:                       # 我要写 ⇒ 对方读或写都不行
            for b in h_writes | h_reads:
                if _touch(a, b):
                    return True
        for a in reads:                        # 我要读 ⇒ 对方写不行（对方读可以）
            for b in h_writes:
                if _touch(a, b):
                    return True
    return False


def _keys(ctx: ToolContext, spec: "SubAgent") -> tuple[set[str], set[str]]:
    prefix = str(ctx.project) if ctx.project is not None else "<未绑定>"
    return ({f"{prefix}::{p}" for p in spec.reads},
            {f"{prefix}::{p}" for p in spec.writes})


def missing_requires(ctx: ToolContext, spec: "SubAgent") -> list[str]:
    """还缺哪些硬前置（空 = 可以开工）。

    ⚠️ 这道检查的目的不是"拦"，是**别让子代理白跑**：
    缺前置是**确定性可判**的，交给模型去发现要花掉一整轮工具调用和一次模型调用，
    还会给回一句含糊的"做不到"。不如在这里就地拒绝，把"该先做什么"直接告诉主管。
    """
    if ctx.project is None:
        return []          # 没绑项目单独判（见 runner 里的 no_project 分支），别混进"缺文件"
    return [rel for rel in spec.requires if not (ctx.project / rel).exists()]


def would_block(ctx: ToolContext, spec: "SubAgent") -> bool:
    """现在进去会不会排队（只用来给界面报一句"排队中"，不改行为）。"""
    reads, writes = _keys(ctx, spec)
    with _GATE:
        return _blocked(reads, writes)


@contextlib.contextmanager
def _resource_gate(ctx: ToolContext, spec: "SubAgent"):
    """把这次子代理执行放进资源闸门。

    **全有或全无**地拿锁（一次判定、不部分持有）⇒ 天然无死锁：不会出现
    "甲握着 A 等 B、乙握着 B 等 A"。代价是可能稍微少一点并发度，但这个规模下无所谓。
    """
    reads, writes = _keys(ctx, spec)
    owner = f"{spec.title}#{next(_SEQ)}"
    with _GATE:
        while _blocked(reads, writes):
            _GATE.wait()
        _HELD.append((owner, reads, writes))
    try:
        yield
    finally:
        with _GATE:
            _HELD[:] = [h for h in _HELD if h[0] != owner]
            _GATE.notify_all()


# ============================================================================
#  把子代理包成主管能调的工具
# ============================================================================
def build_delegate_tools(ctx: ToolContext) -> list[StructuredTool]:
    """造出 4 个 delegate 工具。每个工具的调用 = 派一个子代理去干活。"""
    seq = itertools.count(1)
    cache: dict[str, Any] = {}

    def graph_for(spec: SubAgent) -> Any:
        """子代理的图只建一次，复用（无 checkpointer ⇒ 每次调用都是全新的一次）。

        为什么复用而不是每次重建：`create_react_agent` 会把工具 schema 编译一遍，
        每轮都建纯属浪费。复用不影响隔离 —— 隔离靠的是"不挂 checkpointer"，
        每次 `.stream()` 都是干净的一次会话。
        """
        g = cache.get(spec.name)
        if g is None:
            g = create_react_agent(
                build_chat(ctx.config.model),
                build_tools(ctx, only=set(spec.tools)),
                prompt=spec.prompt,
            )
            cache[spec.name] = g
        return g

    def runner(spec: SubAgent):
        def delegate(task: str) -> dict:
            """（描述见 tool description，这里不写文档串避免两处漂移。）"""
            task = str(task or "").strip()
            if not task:
                return {"ok": False, "承担者": spec.title, "cause": "empty_task",
                        "说明": "交过去的任务描述是空的，请说清要它做什么。"}

            # 硬前置不满足 ⇒ 连子代理都不启动。见 `missing_requires`。
            if ctx.project is None:
                return {
                    "ok": False, "承担者": spec.title, "cause": "no_project",
                    "说明": f"{spec.title} 现在做不了：这个会话还没绑定项目。",
                    "下一步": "先 `list_projects` 看有哪些项目，用 `use_project` 绑定；"
                              "或者请用户先传一个视频。",
                }
            missing = missing_requires(ctx, spec)
            if missing:
                return {
                    "ok": False, "承担者": spec.title, "cause": "missing_prerequisite",
                    "缺什么": missing,
                    "说明": f"{spec.title} 现在做不了：缺 " + "、".join(missing) + "。",
                    "下一步": spec.blocked_hint or "先补上前置，再派它。",
                }

            sub_id = f"sub{next(seq)}"
            ctx.tell(type="sub_start", id=sub_id, tool=spec.name,
                     agent=spec.title, task=task)

            trace: list[str] = []
            final = ""
            failure: str | None = None
            try:
                graph = graph_for(spec)
                # 排不排队要**说出来** —— 否则用户只看到"卡住了"，不知道是在等别人。
                if would_block(ctx, spec):
                    ctx.tell(type="sub_wait", id=sub_id, agent=spec.title,
                             reason="和另一个正在跑的子代理有产物冲突，先排队等它（不是卡住）")
                # ⚠️ 整个子代理执行都过「资源闸门」：只挡**产物冲突**，不挡"同项目"。
                #    两个子代理只要读写不相交就能同时跑 —— 例如"烧字幕 ∥ 渲染动效"。
                #    依据见 `_resource_gate` 与各 spec 的 reads/writes 声明。
                with _resource_gate(ctx, spec):
                    for chunk in graph.stream(
                        {"messages": [HumanMessage(content=task)]},
                        {"recursion_limit": SUB_RECURSION_LIMIT},
                        stream_mode="updates",
                    ):
                        for _node, upd in (chunk or {}).items():
                            for msg in ((upd or {}).get("messages") or []):
                                if isinstance(msg, AIMessage):
                                    for tc in (getattr(msg, "tool_calls", None) or []):
                                        nm = str(tc.get("name") or "?")
                                        trace.append(nm)
                                        ctx.tell(type="sub_step", id=sub_id, tool=nm,
                                                 args=tc.get("args") or {})
                                    text = text_of(msg).strip()
                                    if text:
                                        final = text
                                elif isinstance(msg, ToolMessage):
                                    body = text_of(msg)
                                    ok, cause = _verdict(body)
                                    ctx.tell(type="sub_step_result", id=sub_id,
                                             tool=getattr(msg, "name", "?") or "?",
                                             ok=ok, cause=cause,
                                             content=_clip(body, SUB_STEP_LIMIT))
            except Exception as e:  # noqa: BLE001 —— 子代理崩了不能把主管也带崩
                failure = f"{type(e).__name__}: {e}"

            ok = bool(final) and failure is None
            ctx.tell(type="sub_end", id=sub_id, tool=spec.name, agent=spec.title, ok=ok)

            if failure is not None:
                return {"ok": False, "承担者": spec.title, "cause": "subagent_error",
                        "说明": f"{spec.title} 执行中出错：{failure}",
                        "它调用了": trace}
            if not final:
                return {"ok": False, "承担者": spec.title, "cause": "no_report",
                        "说明": (f"{spec.title} 跑完了但**没有交回结论**"
                                 f"（调用了 {len(trace)} 个工具）。"
                                 f"可以是它卡住了 —— 换了说法再说一次，或换个做法。"),
                        "它调用了": trace}

            out: dict[str, Any] = {"ok": True, "承担者": spec.title,
                                   "它调用了": trace, "结论": _clip(final, SUB_REPORT_LIMIT)}
            if not trace:
                out["注意"] = ("它没有调用任何工具就交回了结论 —— "
                               "如果结论里提到做过什么，那是它编的，请核实。")
            return out

        return StructuredTool.from_function(
            func=delegate, name=spec.name, description=spec.brief)

    return [runner(s) for s in SUBAGENTS]


# ⭐ 知识包工具**统一拼给每个子代理**（不是逐个手写）：
#    新增子代理时自动带上；也不会出现"4 处改了 3 处"那种漏。
#    ⚠️ 提示词尾巴也一起拼 —— 光给工具不说，模型多半想不起来去读。
SUBAGENTS: tuple[SubAgent, ...] = tuple(
    replace(s, tools=tuple(s.tools) + SKILL_TOOLS, prompt=s.prompt + SKILL_NOTE)
    for s in _BASE
)


def roster() -> list[dict[str, Any]]:
    """给界面/自检看的编制表（谁管什么、拿哪些工具）。"""
    return [{"tool": s.name, "title": s.title, "tools": list(s.tools)} for s in SUBAGENTS]
