"""对话主管 —— 用户用话安排任务的那一层。

它做的事只有一件：**听懂用户要什么，然后挑最少的工具把它做成**。

为什么不是一个更"聪明"的提示词就够：主管的"聪明"全在**它的工作纪律**里 ——
先看状态、只做要的那件事、缺前置自己补、做不到就说做不到、花钱先问。
提示词把这几条写死，它才不会退化成"每次把全流程跑一遍"。
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from ..config import AppConfig
from ..kernel import KernelClient
from ..llm import build_chat
from .subagents import build_delegate_tools, text_of as _text_of
from .tools import ToolContext, build_tools

# 一轮对话里最多允许多少步（含工具调用）。默认 25 对"转写→字幕→烧测试→审→烧全片"
# 这种五六步的活够用，但用户如果连说几件事就容易顶到上限；给宽一点。
RECURSION_LIMIT = 60

# 同一个工具、**同样的参数**在一轮里最多重复几次。
# 为什么必须有这道护栏：agent 陷进循环时**不报错**，只是安静地重复调用，
# 用户看到的是"一直在转圈"，账单和时间却在真实地烧。宁可主动叫停并说明，
# 也不要让它跑到 recursion_limit 再甩一句 "Sorry, need more steps"。
SAME_CALL_LIMIT = 3

# 主管**自己留着**的工具。字幕/剪裁/动效/质检那 16 个不在这里 —— 它们在子代理手上。
#
# 判据是"这件事跨不跨角色"：
#   · 编排类（看项目、切项目、跑全流程）→ 主管自带
#   · 成片装配（定规格 / 生 BGM / 混音）→ 主管自带，因为 **BGM 是唯一花钱的一步**，
#     把它留在主管手里，子代理就根本看不见付费能力（第一道闸）。
#   · 单一专业领域的活 → 全给对应的子代理，主管只派活
DIRECTOR_TOOLS: frozenset[str] = frozenset({
    "list_projects", "use_project", "inspect_project",
    # ⭐ 转写留在主管手里：它是**三个下游的共同素材**（剪辑判断 / 字幕 / 动效都要它），
    #    而且不含判断（纯确定性转写），不需要"专业人士"。放在某个子代理名下会制造假依赖 ——
    #    曾经"给视频加动效"必须先派剪裁策划，就是为了让它去转写。
    "transcribe",
    "prepare_final_specs", "generate_bgm", "mix_audio",
    "run_standard_pipeline",
})


SYSTEM = """\
你是「口播剪辑主管」。用户用一句话安排任务，你负责**派人把它做完**。

**当前绑定的项目：{project}**
（可以中途换 —— 用户提到别的项目、或你发现绑错了，用 `list_projects` + `use_project`。）

## 你的编制：你负责「素材 + 编排」，**专业活派出去**

你自己动手的只有两类：**备齐公共素材**（`transcribe`）+ **编排与把关**
（看/切项目、定规格、混音、装配、派活、汇报）。除此之外一律派活。

你手下有 **4 个专业子代理**，每个都包成了一个工具。**调用它们 = 派活**：

  · `delegate_transcript_editor`（剪裁策划）
     管：看清每一句、判断删哪些段、执行剪切、建立时间轴。
     **不碰**转写（那是你的活）/ 字幕 / 动效 / 质检。
  · `delegate_subtitle_maker`（字幕制作）
     管：字幕文本、生成、烧录、排版复核。**不碰**剪辑 / 动效 / BGM。
  · `delegate_overlay_designer`（特效设计）
     管：动效工程的设计、渲染、叠到成片上。**不碰**字幕 / 剪辑 / BGM。
  · `delegate_qc_inspector`（成片质检）
     管：跑质检断言、抓帧看图、读产物。"这版到底行不行"就派它。

**你自己留着 8 个工具**，都是编排、素材与跨角色的事，**不要派出去**：
`list_projects` `use_project` `inspect_project`（看）·
**`transcribe`（转写 —— 三个下游的共同素材，你自己做，别派）** ·
`prepare_final_specs` `generate_bgm` `mix_audio`（成片装配，含唯一花钱的一步）·
`run_standard_pipeline`（标准全流程，只在用户明确说"完整出一版"时用）

## ⛔ 第 0 条：这四类**专业活**一律派出去，别自己上手

字幕、动效、剪裁、质检 —— 这四类活**一律派给对应的子代理**。
哪怕你觉得"就一步、我自己更快"，也派。理由不是礼貌，是三条实际的：

1. **上下文**：子代理各自吃掉大堆中间产物（光一份句子表就 2900 字符），
   只把**结论**交回你。你省下的上下文，就是后面还能好好判断的余量。
2. **白名单**：它们看不见不该碰的工具（字幕制作根本看不见花钱的 BGM）。
3. **专业分工**：每个子代理带着自己那一套规矩（比如烧字幕必须先烧测试片看一眼）。

### 但它们**不是一条流水线**，你是按需挑人，不是把四道工序走一遍

**四个子代理是四张牌，一次通常只打一张。**
绝大多数请求只碰一个领域 —— 那就只派那一个，其余三个**根本不该被惊动**：

    帮我加字幕      ⇒ 只派「字幕制作」（没逐字稿先自己 transcribe）
    把第 3 句删掉    ⇒ 只派「剪裁策划」（同上）
    加个开场动效     ⇒ 只派「特效设计」（同上 —— **不用先剪、也不用先建时间轴**）
    这版能用吗      ⇒ 只派「成片质检」

⛔ **任何情况下都不要"顺便"把四个都派一遍。** 那是把 agent 又变回固定流水线了，
而且每一张牌背后都是分钟级的真实计算（转写/渲染/烧全片）。
派第二个的前提只有一个：**用户这件事确实横跨两个领域**（比如"加字幕再加动效"）。
派完一个就停下来看结果，别预先排好队。

派活之前，一句话说清"我派谁去干什么"，用户才知道你没在瞎跑。

### ⚠️ 先看清依赖 —— 别把"都跟时间有关"当成"都得先剪"

**唯一的前置是逐字稿，而转写是你自己的活**：

    你：`transcribe`（一次就够，所有下游共用它）
        ├─► 剪裁策划   要看逐字稿才能判断删哪句（**用户要剪才派**）
        ├─► 字幕制作   要按「保留块」重映射 + 烧录得有底片 ⇒ 没剪过片就得先建时间轴
        └─► 特效设计   **只要时间戳** ⇒ 逐字稿就够了
                       （没剪过片时，逐字稿的秒数本来就等于成片秒数，
                        `design_overlay` 会自己把时间轴补到位）

⇒ **「加动效」不用先派剪裁策划**：转写完成后直接派「特效设计」。
⇒ **「加字幕」**要烧到视频上，所以还需要时间轴（未剪 ⇒ 零删除建一份）。
⇒ 两件都要时：先派「剪裁策划」建时间轴（零删除，一个字不动），
  回来后再把「字幕制作」+「特效设计」**放在同一条消息里**一起派 ——
  这一步是**真并行**（渲染动效只读 `overlays/index.html`、**不读任何视频**，
  和"烧字幕"互不干扰），省掉一次完整等待（烧全片几十秒、渲染动效两分钟）。

⚠️ 一句话版本：**动效依赖的是"时间戳"，不是"剪裁"**。
   凡是"给某个时间点加特效"的请求，都别去惊动剪裁策划。
   两件都回来之后，再走装配（你手上的 `prepare_final_specs` / `mix_audio` / `apply_overlay`）。

⛔ 缺前置会被**当场拒绝**（`missing_prerequisite`），工具返回里会写清"该先做什么"。
   那不是失败，是在提醒你顺序反了 —— 照它说的补，别绕。
⚠️ 其余组合也有前后依赖（叠加要垫在混音成片上），派出去只会排队。
   服务端会自动判断，冲突的排队并在界面上显示"排队中" —— 所以你不必怕派错，
   但也别指望"全派一遍"能变快。

## ⚠️ 派活时**必须把用户原话带上**

子代理**看不到**用户那句话 —— 它只看到你写的 `task`。所以 task 里必须写清：

  · **用户要什么**：把用户的说法**原样**带进去。
    ⛔ 别转述成"生成字幕"这种抽象词 —— 用户说"把开头两秒的废话去掉"，
    差"开头两秒"这四个字，结果就完全不同。
  · **现状**：这个项目到哪一步了、你已经确认过什么。
  · **界限**：用户提过的约束一定要写进去（"别删内容""只改这一句""字幕别动"）。

⛔ 只写"帮我加字幕"是不够的 —— 那等于把用户的原话丢了。

## 你的其余工作纪律

**1. 先看清楚，再派活。**
· 没绑定项目 ⇒ `list_projects` 看有哪些，用 `use_project` 绑定（或请用户上传视频）。
· 绑定了 ⇒ 先 `inspect_project` 看清已有什么、缺什么。不要凭记忆或假设。

**2. 只做用户要的那件事。**（最重要的一条）
用户说"帮我加字幕"，就派字幕制作做完，然后**停**。
⛔ 不要顺手把裁剪、动效、BGM、质检都跑一遍 —— 每一段都是分钟级的真实计算
（转写 75 秒素材要 24 秒、烧全片几十秒、渲染动效两分钟），用户没要的就是白干。
`run_standard_pipeline` 只在用户明确说"完整出一版""按标准流程来一遍"时才用。

**3. 花钱必须请用户授权。**
`generate_bgm` 是唯一花钱的一步，**在你手上**（不在任何子代理手里）。
它若返回 `needs_authorization`，就把价格如实告诉用户、请他在界面上点「授权本次付费」。
⛔ **绝对不要**自己认为"这点钱没关系"。你也没有任何工具能替用户授权。

**4. 子代理说做不到，就照实转达。**
任何子代理返回 `ok=false` 时，把它给的原因**如实**告诉用户，并给出最接近的替代方案。
⛔ 不要自己编一个看起来像做了的答案，也不要假装某个文件已经产出。
⚠️ 子代理返回里若带一条 `注意`，说它**没调用任何工具就交回了结论** ——
那意味着它在编。必须自己去核实（`inspect_project` 或让质检读产物），不要直接转述。

**5. 汇报要具体。**
做了什么、**谁做的**、产出了哪个文件、哪一步没做、下一步能做什么。
⛔ 不要只说"已完成" —— 用户要能自己复核。

## 一些事实（省得你瞎猜）
- 项目根目录：`{projects_root}`
- 源片固定放在 `input/raw.mp4`，这是内核认的唯一路径。
- 交付物固定是 `renders/final.mp4`；无特效版是 `renders/assembled.mp4`。
- 动效是**透明叠加层**（ProRes4444 带 alpha），不是直接改视频。
- 渲染动效要求项目 `overlays/node_modules` 下装了 HyperFrames，没装会报 `missing_hyperframes`。

## 说话方式
像同事一样说话：简洁、直接、不客套。用户要的是"做成了没有"，不是寒暄。
可以正常聊天（用户问"你能做什么"就如实说你的编制）。
要派活之前，一句话说清你打算派谁去干什么。
"""


@dataclass
class Director:
    agent: Any
    ctx: ToolContext
    checkpointer: Any

    def cfg(self, session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id},
                "recursion_limit": RECURSION_LIMIT}


def make_director(config: AppConfig, project: Path | None = None,
                  client: KernelClient | None = None,
                  checkpointer: Any | None = None) -> Director:
    """造一个主管。同一个 session_id 的多轮会自动记住上下文。

    `project` 允许为 `None`（会话还没绑定项目）—— 这正是"自由对话"的前提。
    之后可以用 `use_project` 工具切换；提示词里那句"当前绑定的项目"是**每轮现算**的
    （`prompt` 传函数而不是字符串），所以换项目不必重建 agent，也就不会丢对话记忆。

    `checkpointer` 可注入：默认内存态（测试用），服务端传持久化 Saver，
    这样**重启服务后对话记忆还在**。
    """
    ctx = ToolContext(project=project, config=config,
                      client=client or KernelClient(config))
    # 主管的工具箱 = 编排类 7 个 + **4 个 delegate 工具**（每个 = 一个子代理）。
    # 那 16 个业务工具进了子代理的白名单，主管自己看不见 —— 这是"派活"的机制保证：
    # 不是靠提示词请求它别自己干，而是它**根本没有那些工具**。
    tools = build_tools(ctx, only=set(DIRECTOR_TOOLS)) + build_delegate_tools(ctx)
    model = build_chat(config.model)
    cp = checkpointer if checkpointer is not None else InMemorySaver()

    def _prompt(state: Any) -> list:
        """每轮现算的系统提示词（项目可能中途被换掉）。

        ⚠️ **必须把 `state["messages"]` 一起返回** —— 这里有个非常隐蔽的坑：
        `create_react_agent` 的 `prompt` 传函数时，它的返回值是**这次要喂给模型的
        完整消息列表**，不是"系统提示词"。只返回 SystemMessage 会把对话历史整个清掉，
        于是模型每一步看到的输入**完全一样** ⇒ 同样的判断、同样的工具调用 ⇒
        确定性地陷进死循环（实测：连调 26 次 `list_projects`，一句人话都不说，
        而且不报错、只是在烧时间和钱）。
        这不是"提示词写法"问题，是**契约理解错**：函数的语义是 replace，不是 prepend。
        """
        p = ctx.project
        return [SystemMessage(content=SYSTEM.format(
            project=f"{p.name}（目录 {p}）" if p is not None else "还没有绑定任何项目",
            projects_root=str(config.projects_root),
        ))] + list(state.get("messages") or [])

    agent = create_react_agent(model, tools, prompt=_prompt, checkpointer=cp)
    return Director(agent=agent, ctx=ctx, checkpointer=cp)


# ============================================================================
#  把一轮对话变成界面事件
# ============================================================================
# `_text_of` 从 subagents 借 —— 两边各写一份必然漂移（这里已经有一次教训了：
# "同一件事两处实现"，最后一定是改了一处、另一处悄悄不一致）。
def _bump(seen: dict[str, int], name: Any, args: Any) -> None:
    """记一次工具调用；**同一工具 + 完全相同的参数**超过 `SAME_CALL_LIMIT` 次就抛。

    为什么按"参数也相同"来判：同一个工具在一轮里被合理地调多次是正常的
    （不同项目、不同 mode）。真正是病的是**一字不差的重复** —— 它意味着
    模型没有从上次结果里得到任何新信息，接下来只会再来一遍。
    """
    key = f"{name} {json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"
    seen[key] = seen.get(key, 0) + 1
    if seen[key] > SAME_CALL_LIMIT:
        raise _LoopGuardTripped(str(name), seen[key])


class _LoopGuardTripped(Exception):
    def __init__(self, tool: str, count: int) -> None:
        super().__init__(tool)
        self.tool, self.count = tool, count


def stream_turn(director: Director, session_id: str, message: str) -> Iterator[dict]:
    """跑一轮对话，逐个产出界面事件。

    事件类型：
      · `assistant_delta` —— 主管正在说的话，**逐字**出来（气泡实时长文字）
      · `assistant`       —— 这一段说完后的**完整文本**（权威值，前端据此校正气泡）
      · `tool_call`       —— 主管决定调哪个工具、参数是什么（让过程可见）
      · `tool_result`     —— 那个工具回了什么（**这是"它真的做了事"的证据**）
      · `sub_*`           —— 子代理内部的动作（由 `ctx.emit` **实时**汇入）
      · `done`            —— 这一轮结束

    为什么两种文本事件都要：`assistant_delta` 只为"看着像在说话"，
    但它是**分片拼的**，边界可能被切断、也可能因为网关重试而重复；
    所以每段说完再发一条 `assistant` 作为定稿，前端拿它覆盖，不会留下错字。

    ## 为什么要把 agent 跑到后台线程、再汇进一条队列

    子代理是**嵌套执行**的：它在自己那个 ReAct 循环里调了哪些工具，
    根本不会出现在主管的消息流里。挂 `ctx.emit` 能把它们掏出来，
    但如果只在"主管下一个 chunk 到达时"才倒出去，那些事件就会**被憋在缓冲区里** ——
    实测过一个 43 秒的子代理：`sub_start` 本该在 6 秒时出现，却拖到 49 秒才跟结果一起冒出来。
    那样"过程可见"就是句空话，用户只能干等到最后。

    所以让 agent 在后台线程里跑，**所有事件（含 `ctx.emit` 掏出来的子代理事件）
    直接进同一条线程安全队列**，这里只负责往外吐。这样事件一到就出去，是真流式。
    """
    q: "queue.Queue[dict | None]" = queue.Queue()

    def worker() -> None:
        try:
            for ev in _run_turn(director, session_id, message, q):
                q.put(ev)
        except Exception as e:  # noqa: BLE001 —— 任何异常都要变成用户能看懂的一句话
            q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            q.put({"type": "done", "notes": list(director.ctx.notes)})
            q.put(None)                      # 哨兵：告诉消费者"没有了"

    threading.Thread(target=worker, daemon=True, name="director-turn").start()
    while True:
        ev = q.get()
        if ev is None:
            return
        yield ev


def _run_turn(director: Director, session_id: str, message: str,
              q: "queue.Queue[dict | None]") -> Iterator[dict]:
    """真正干活的那段（在后台线程里跑）。"""
    yield {"type": "turn_start", "message": message}
    seen_calls: dict[str, int] = {}

    # 子代理的嵌套动作**直接进同一条队列**，不经过缓冲区 ⇒ 一到就出去。
    # ⚠️ `queue.Queue.put` 是线程安全的，两个子代理并发时事件会交错，
    #    但每条都带 `id`，前端按 id 归位，不会串。
    director.ctx.emit = q.put
    try:
        stream = director.agent.stream(
            {"messages": [HumanMessage(content=message)]},
            director.cfg(session_id),
            # 两个流一起要：messages 给逐字，updates 给工具调用与定稿
            stream_mode=["updates", "messages"],
        )
        for item in stream:
            if isinstance(item, tuple) and len(item) == 2:
                mode, chunk = item
            else:                       # 兜底：万一只回一种流
                mode, chunk = "updates", item

            if mode == "messages":
                msg = chunk[0] if isinstance(chunk, tuple) else chunk
                if isinstance(msg, AIMessageChunk):
                    text = _text_of(msg)
                    if text:
                        yield {"type": "assistant_delta", "text": text}
                continue

            for _node, upd in (chunk or {}).items():
                for msg in ((upd or {}).get("messages") or []):
                    if isinstance(msg, AIMessage):
                        for tc in (getattr(msg, "tool_calls", None) or []):
                            name, args = tc.get("name"), tc.get("args") or {}
                            _bump(seen_calls, name, args)      # 超限会抛，见上
                            yield {"type": "tool_call", "name": name, "args": args}
                        text = _text_of(msg).strip()
                        if text:
                            yield {"type": "assistant", "text": text}
                    elif isinstance(msg, ToolMessage):
                        yield {"type": "tool_result",
                               "name": getattr(msg, "name", "?") or "?",
                               "content": _text_of(msg)}
    except _LoopGuardTripped as e:
        yield {"type": "error", "message":
               f"我把它叫停了：`{e.tool}` 用**完全相同的参数**连续调了 {e.count} 次，"
               f"看起来是陷进了循环（不报错、只是安静地重复，时间和钱照样烧）。"
               f"换个说法再说一次，或者直接告诉我该用哪个项目。"}
    except Exception as e:  # noqa: BLE001 —— 任何异常都要变成用户能看懂的一句话
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
    finally:
        # 一定要摘掉：这个 ctx 属于**长期持有**的会话，
        # 留着回调等于把一条已经没人读的队列挂在会话上（下一轮会往里塞到没人看的地方）。
        # ⚠️ `done` 不在这里发 —— 由 `stream_turn` 的 worker 发（那样"异常也要收尾"只有一处）。
        director.ctx.emit = None
