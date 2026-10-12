"""对话主管的**工具集** —— 把内核的确定性阶段，一道道摆成能点的菜。

## 为什么要有这个文件

原来的 `langgraph_app` 把 11 个内核阶段串成了一条**固定链**（19 步，一路跑到交付）。
那条链本身是对的 —— 它是"标准全流程"，也作为**一个工具**保留在这里
（`run_standard_pipeline`）。但用户要的不是"只能跑套餐"，而是"能点菜"：

    帮我加字幕   → 转写 → 生成字幕 → 烧字幕
    帮我加特效   → 设计动效 → 渲染动效 → 叠加
    帮我裁剪     → 转写 → 判断删留 → 执行剪切

三件事共用两个前置（转写），但谁都不需要跑完整条链。**所以粒度按"内核的确定性阶段"切**，
让主管 agent 自己按目标挑、自己补前置。

## 三条设计纪律

1. **每个工具的 description 只写事实**：干什么 / 要什么前置 / 产出什么 / 要不要花钱 / 大概多久。
   ⛔ 不写"你应该先调 X 再调 Y"这类流程指令 —— 那是主管的判断，写死就等于把 agent 又变回工作流。
2. **LLM 侧步骤不重写提示词**：判断删留 / 设计动效 / 审片这些都直接**调用图里那个节点函数**一次
   （`_run_node_once`），文件副作用与真跑完全一致。各写一份提示词必然漂移。
3. **花钱的动作不在本层放行**：`generate_bgm` 只看服务端会话里的授权位，
   **agent 无权写那个位**（见 `session.paid_ok`）。这条是铁律："不可逆动作的闸门，
   禁止押在会自己说话的东西上"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from .. import stages as S
from ..config import AppConfig
from ..kernel import KernelClient, KernelResult
from ..nodes import build_nodes
# 与图里那些节点**同一套**原子写（tmp + os.replace）：半截 JSON 比没有文件更难查。
from ..nodes.base import write_json

# 允许 agent 读的产物（白名单，不是黑名单 —— 让"能读什么"是可枚举的）
READABLE = {
    "transcript": "edit/transcript.json",          # 剪前逐字稿（word 级时间戳）
    "timeline": "edit/timeline_map.json",          # 剪后时间轴（音画同步的依据）
    "plan_context": "edit/plan_context.json",      # 剪辑证据（模型判读用）
    "edit_decisions": "edit/edit_decisions.json",  # 删留决策
    "cut_blocks": "edit/cut_blocks.json",          # 保留块（切点）
    "subtitle_fixes": "edit/subtitle_fixes.json",  # 字幕文本修复
    "final_srt": "edit/subtitles/final.srt",       # 成品字幕
    "bgm_spec": "edit/bgm.json",
    "mix_spec": "edit/mix.json",
    "assemble_spec": "edit/assemble.json",
    "overlay_html": "overlays/index.html",         # 动效工程
    "qc_report": "edit/qc_report.json",
    "pipeline_report": "edit/pipeline_report.json",
}


@dataclass
class ToolContext:
    """一次对话所绑定的一切：项目、配置、以及**只有服务端能改**的授权位。"""

    # ⚠️ 允许为 None：会话刚开时不一定有项目 —— 这正是"自由对话"的前提，
    #    不能让"先在下拉框里选个项目"成为开口说话的条件。
    #    项目由 `use_project` 工具（或上传动作）写入，工具按**调用时**解析，
    #    见 `_LiveProject`。
    project: Path | None
    config: AppConfig
    client: KernelClient
    # ⛔ 授权位：由前端按钮写入，agent 无任何工具能改它。
    #    这是"付费闸门"的实际落点 —— 不押在模型身上，也不押在框架语义上。
    paid_ok: bool = False
    notes: list[str] = field(default_factory=list)
    # 本次会话里各阶段的内核结果（**原始形状**，含 report.frames 等证据）。
    #
    # ⚠️ 为什么必须有它：LLM 侧节点（审片 / 定规格 / 执行叠加…）要从
    #    `state["results"]` 里读**上游阶段的证据**（例如审片要读烧测试片给的抓帧路径）。
    #    若给它们一个空的 results，那些节点就会掉进"兜底扫描 edit/tmpimg"的分支 ——
    #    而那条分支的注释自己就写着"**不保证**是本轮产生的帧"。
    #    拿不确定的证据下断言，正是本项目最忌讳的一类错误（断言看着在跑、证据是错的）。
    #    所以这里把每次内核调用的原始结果记下来，喂回给节点用。
    last_results: dict[str, dict] = field(default_factory=dict)

    # 子代理内部动作往外吐的通道。由 `director.stream_turn` 挂上；直接调工具时是 None。
    #
    # ⚠️ 为什么必须有：子代理是**嵌套执行**的 —— 它在自己的 ReAct 循环里调了哪些工具，
    #    不会出现在主管的消息流里。不挂这条通道，界面上就只剩一句
    #    "delegate_subtitle_maker 完成"，用户看不到里面发生过什么。
    #    而"过程可见"是这个项目从一开始就守的一条（工具卡就是证据）。
    emit: Any = None

    def tell(self, **event: Any) -> None:
        """把一次子代理内部动作交给上层（没人听就静默丢弃）。"""
        if self.emit is not None:
            self.emit(event)

    def record(self, node: str, raw: dict) -> None:
        self.last_results[node] = raw


class NoProjectError(RuntimeError):
    """会话还没绑定项目，而这一步没有项目就做不了。"""


class _LiveProject:
    """一个**活的**项目路径：每次被用到时才去 `ctx` 取当前绑定的项目。

    ## 为什么需要它（而不是在 `build_tools` 里 `proj = ctx.project` 一次取完）

    同一个会话**可以中途换项目** —— 用户传了新视频，或者说"看看另一个项目"。
    若把项目在构造工具时就焊死，换项目就得重建整个 agent，而重建会连带丢掉
    模型侧的对话记忆（`create_react_agent` 的提示词与工具都是构造期固化的）。
    这与"能自由对话"直接冲突，所以项目必须**按调用时解析**。

    ## 为什么用转发而不是把 `ctx` 暴露出去

    工具函数里的写法保持原样：`cli.transcribe(proj)`、`proj / "edit/xxx"`、
    `proj.name` —— 只要 `__fspath__` / `__str__` / `__truediv__` 和属性转发齐了，
    调用方根本看不到这一层。少一次机械改写，就少一次改错的机会。

    ## 未绑定时的行为

    **立刻抛 `NoProjectError`**，绝不静默拼出一个错误的绝对路径 ——
    "看着在跑、其实在操作别的地方"比直接报错危险得多（本项目吃过这个亏）。
    异常会被工具层转成一句中文提示交回给主管，它就有机会先 `use_project`。
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def _real(self) -> Path:
        p = self._ctx.project
        if p is None:
            raise NoProjectError(
                "这个会话还没有绑定项目。先调 list_projects 看有哪些项目可选，"
                "再用 use_project 绑定一个；或者请用户上传一个视频（上传后会自动绑定）。"
            )
        return Path(p)

    # ---- Path 协议（只实现真正会被用到的那几个）----------------------------
    def __fspath__(self) -> str:      # subprocess / os.fspath
        return str(self._real())

    def __str__(self) -> str:         # kernel.py 里是 str(project)
        return str(self._real())

    def __repr__(self) -> str:
        p = self._ctx.project
        return f"<_LiveProject {'未绑定' if p is None else p}>"

    def __truediv__(self, other: Any) -> Path:   # proj / "edit/x.json"
        return self._real() / other

    def __getattr__(self, name: str) -> Any:     # .name / .is_file() / .glob() …
        return getattr(self._real(), name)


# ============================================================================
#  小工具
# ============================================================================
def _ok(result: KernelResult, ctx: ToolContext | None = None, node: str | None = None) -> dict:
    """把内核结果压成 agent 好读的 JSON（**不截断 cause / 错误**，那是它判读的依据）。"""
    rep = result.report or {}
    if ctx is not None and node:
        # 记下**原始**结果（含证据路径），供后面的 LLM 侧节点当 state 用
        from ..state import result_to_dict

        ctx.record(node, result_to_dict(result))
    out: dict[str, Any] = {
        "ok": result.ok,
        "耗时秒": round(result.duration_s, 1),
    }
    if result.cause:
        out["cause"] = result.cause
    if result.causes and list(result.causes) != ([result.cause] if result.cause else []):
        out["causes"] = list(result.causes)
    if not result.ok:
        # ⚠️ 失败时必须把**现场**交出去（错误 / 提示 / 输出尾巴）：
        #    只说"失败了"等于让 agent 没法判断下一步，它会盲试。
        for k in ("error", "hint", "missing", "price_per_call", "causes"):
            if k in rep:
                out[k] = rep[k]
        if result.stderr_tail:
            out["stderr尾"] = result.stderr_tail[-600:]
    # 通过时只回少数几个有用的数字，别把整份报告灌进去
    for k in ("next_stage", "last_qc_ok", "last_qc_causes", "segment_count",
              "duration", "output", "bytes", "cost", "price_per_call", "skipped",
              "subtitle_count", "checks_failed", "blocks"):
        if k in rep and out.get(k) is None:
            out[k] = rep[k]
    # 抓帧证据：只报**有几张**，不把整串绝对路径灌给模型（它会当成要念出来的东西）
    for k in ("frames", "junction_frames"):
        if rep.get(k):
            out[f"{k}张数"] = len(rep[k])
    if result.summary:
        out["摘要"] = result.summary
    return out


def _run_node_once(ctx: ToolContext, node: str, goal_extra: dict | None = None) -> dict:
    """**调用图里那个 LLM 节点一次**，拿它的副作用（写文件）与结果。

    为什么不另写一份提示词：`llm_nodes.py` 里那些提示词是这个项目最花心思的部分
    （契约逐条对齐 lint、失败反馈怎么喂、证据怎么给）。复制一份到 agent 层必然漂移，
    而漂移的后果恰好最糟：**agent 走的路与流水线走的路不是同一条**，
    于是"我这边能跑通、那边跑不通"这种最难查的问题就会出现。
    """
    from langgraph_app.state import PipelineState
    from langgraph.types import Command

    if ctx.project is None:
        return {"ok": False, "cause": "no_project",
                "error": "会话还没绑定项目，这一步做不了。先 use_project 或让用户上传视频。"}

    nodes = build_nodes(ctx.client, ctx.config)
    fn = nodes.get(node)
    if fn is None:
        return {"ok": False, "cause": "internal_error", "error": f"没有这个节点：{node}"}

    state = PipelineState(
        project=str(ctx.project),
        goal={"overlay_required": True, "bgm_source": "generate",
              "allow_paid": ctx.paid_ok, "allow_expensive": False,
              **(goal_extra or {})},
        # ⚠️ 把本次会话里已跑过的内核结果喂回去。这不是可有可无的"补全"：
        #    审片类节点要从上游阶段的结果里取**抓帧证据路径**，拿不到就只能
        #    掉进"兜底扫描 edit/tmpimg"——那条路的注释自己写着"不保证是本轮产生的帧"。
        #    宁可不给结果让它明确报缺证据，也不要给它一份来路不明的证据。
        results=dict(ctx.last_results),
        retakes={}, cause_history=[], route_history=[],
        cross_agent_events=[], notes=[], halts=[], approvals={}, revision={},
    )
    try:
        cmd = fn(state)
    except Exception as e:  # noqa: BLE001 —— 节点异常也要变成 agent 能读的结果
        return {"ok": False, "cause": "node_exception",
                "error": f"{type(e).__name__}: {e}"}
    if not isinstance(cmd, Command):
        return {"ok": False, "cause": "internal_error",
                "error": f"{node} 没有返回 Command（实现可能变了）"}

    upd = cmd.update or {}
    data = (upd.get("results") or {}).get(node) or {}
    out = _ok_direct(data, upd)
    for n in (upd.get("notes") or []):
        ctx.notes.append(str(n))
    return out


def _ok_direct(data: dict, upd: dict) -> dict:
    """`_run_node_once` 拿到的是已转成 dict 的结果（不是 KernelResult），单独处理。"""
    if not data:
        # 节点没写 results：多半是"跳过"这种语义分支，把 notes 交出去
        return {"ok": True, "说明": "；".join(str(n) for n in (upd.get("notes") or []))
                or "该步骤未产生结果记录"}
    out: dict[str, Any] = {"ok": bool(data.get("ok"))}
    if data.get("cause"):
        out["cause"] = data["cause"]
    rep = data.get("report") or {}
    if rep.get("skipped"):
        out["说明"] = f"跳过：{rep['skipped']}"
    for k in ("error", "hint", "price_per_call", "cost", "index_html", "bytes",
              "duration", "output", "subtitle_count"):
        if k in rep:
            out[k] = rep[k]
    if data.get("summary"):
        out["摘要"] = data["summary"]
    if not data.get("ok"):
        tail = data.get("stderr_tail") or ""
        if tail:
            out["stderr尾"] = tail[-600:]
    return out


def _read_json(ctx: ToolContext, rel: str) -> Any:
    p = ctx.project / rel
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
#  时间轴：动效要的是**时间戳**，不一定要剪过片
# ---------------------------------------------------------------------------
def _zero_drop_decisions(proj: Path) -> dict:
    """「零删除」的剪辑决策。**唯一实现** —— `establish_timeline` 与动效的时间轴兜底共用。

    ⚠️ 保留已有的 `text_fixes`：改时间轴不该顺手抹掉**文本修正**（两码事）。
    """
    dp = proj / "edit" / "edit_decisions.json"
    prev_fixes: dict = {}
    if dp.is_file():
        try:
            prev_fixes = (json.loads(dp.read_text(encoding="utf-8-sig")) or {}).get("text_fixes") or {}
        except (OSError, ValueError):
            prev_fixes = {}
    return {"source": "input/raw.mp4",
            "authored_by": "主管：零删除，仅用于确定时间轴",
            "drop_segment_ids": [],
            "text_fixes": prev_fixes,
            "notes": "用户未要求删减内容，保留全部"}


def _ensure_timeline(ctx: ToolContext) -> tuple[bool, str]:
    """保证 `edit/timeline_map.json` 存在。返回 `(是否就绪, 说明)`。

    ## 为什么动效**不必**经过剪裁

    动效只消费**时间戳**：`design_overlay` 把秒数写进 GSAP，`render_overlay` 只读
    `overlays/index.html`（**不读任何视频**）。而**没剪过片时** `edit_start ≡ source_start`
    （`_real_person` 那种真剪过的第二块就偏了 5.64 秒），所以逐字稿里的原始秒数
    **就是**成片秒数 —— 逐字稿 + 时间戳已经够了。

    ## 兜底用的是内核现成的「只建时间轴」模式

    `cut-apply`（**不带** `--run`）会算保留块 + 建剪后时间轴 + 落盘，**但不跑 ffmpeg**
    ⇒ 秒级、不产视频、一个画面都不动。
    ⛔ 剪辑的合并规则（连续段且 gap≤5s 才并块）**只有内核那一份实现**，这里一行都不复刻 ——
    复刻的代价是两边迟早不一致，而那是**静默**的错。

    返回的说明会挂进工具返回值（不静默补建）：用户该知道"它为了做动效顺手建了时间轴"。
    """
    if (ctx.project / "edit" / "timeline_map.json").is_file():
        return True, ""                    # 剪过片的真产物 —— 必须用它，不能替换
    if not (ctx.project / "edit" / "transcript.json").is_file():
        return False, ("还没有逐字稿（`edit/transcript.json`）—— 动效的每个时间点都要取自它。"
                       "先 `transcribe`。")
    write_json(ctx.project / "edit" / "edit_decisions.json",
               _zero_drop_decisions(ctx.project))
    res = ctx.client.cut(ctx.project, run=False)      # ← 不跑 ffmpeg
    if not res.ok:
        why = res.cause or (res.report or {}).get("error") or "未知原因"
        return False, f"建时间轴失败：{why}"
    return True, ("本项目原先没有剪辑时间轴 ⇒ 按「零删除」补建了一份"
                  "（不删任何内容、不产视频、不动画面），再用它设计动效。")


# ---------------------------------------------------------------------------
#  知识包（skill）：提示词装「纪律」，skill 装「参考手册」
# ---------------------------------------------------------------------------
# 分工原则（别混）：
#   · **提示词** = 每次都必须遵守的纪律（"先看状态再动手""不要替用户做删减决定"）；
#   · **skill**   = 平时不看、需要时才查的参考数据（lint 规则全集、排版阈值、断言含义）。
# 全塞进提示词 ⇒ 每轮都要为用不到的知识付上下文；全放 skill ⇒ 该守的纪律会被忘掉。
#
# ⚠️ 为什么最终交付物是 `skills/<name>.md` 这种**纯文本**而不是代码：
#    它要被塞进模型的上下文，**人也要能读能改**。加一条规范不该需要改 Python。
#
# 目录是**两级**（扫描顺序决定优先级）：
#   · 仓库级 `<repo>/skills/`      —— 「这套系统怎么干活」，**所有项目共享**；
#   · 项目级 `<项目>/skills/`      —— 这个项目的特殊约定，**同名覆盖**仓库级。
# 为什么要有仓库级：像"lint 怎么跑""质检 16 项断言含义"这类知识，
# 跟具体素材无关，逐项目复制一份只会到处漂移。
_REPO_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
PROJECT_SKILLS_DIR = "skills"


def _parse_skill(path: Path) -> dict:
    """解析一个 skill 文件。frontmatter 用最简单的 `key: value`（**不引入 yaml 依赖**）。

    ⚠️ 解析失败**不能让整个 list 炸掉** —— 一个写坏的 skill 文件不该让 agent 失去
    全部知识。坏文件照样列出来（用文件名兜底当 name），只是标题标成"（frontmatter 无法解析）"，
    这样它至少**看得见**，而不是静默消失。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"name": path.stem, "title": path.stem, "applies_to": "",
                "summary": f"（读取失败：{type(e).__name__}）", "body": ""}

    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            for line in raw[3:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip().lower()] = v.strip()
            body = raw[end + 4:].lstrip("\n")

    broken = not meta
    return {
        "name": meta.get("name") or path.stem,
        "title": meta.get("title") or (f"{path.stem}（frontmatter 无法解析）" if broken else path.stem),
        "applies_to": meta.get("applies_to") or "",
        "summary": meta.get("summary") or "",
        "body": body,
        "path": path,
    }


def _all_skills(ctx: ToolContext) -> list[dict]:
    """合并两级目录；**项目级同名覆盖仓库级**（后扫的赢）。"""
    found: dict[str, dict] = {}
    for d in (_REPO_SKILLS_DIR, ctx.project / PROJECT_SKILLS_DIR):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.is_file():
                s = _parse_skill(p)
                found[s["name"]] = s
    return [found[k] for k in sorted(found)]


# ============================================================================
#  工具集
# ============================================================================
def build_tools(ctx: ToolContext, only: set[str] | None = None) -> list[StructuredTool]:
    """造一次工具集（agent 不需要自己拼路径，也不需要记住项目名）。

    `only` 是**工具白名单** —— 主从多 agent 的关键机制之一：子代理只看得到自己那一份，
    字幕制作根本看不见花钱的 `generate_bgm`。
    ⚠️ 这是"第一道闸"（可见性），**不是**最后一道 —— 付费的真实闸门在服务端授权位
    （见 `generate_bgm` 与 `ToolContext.paid_ok`）。两道都要：白名单防误调，授权位防越权。

    `proj` 是个 `_LiveProject`：**按调用时**解析当前绑定的项目，
    所以会话中途换项目不需要重建工具集，更不需要重建 agent。
    """
    cli, cfg = ctx.client, ctx.config
    proj = _LiveProject(ctx)

    # ------------------------------------------------------------------ 看
    def list_projects() -> dict:
        """列出所有项目，以及每个项目大概做到哪一步了。

        什么时候用它：会话还没绑定项目；或者用户说"那个视频""上次那个"，
        你不知道指哪一个。看完用 `use_project` 绑定。

        ⚠️ 这里只查文件在不在（快，不跑内核）。要精确状态请在绑定后调 inspect_project。
        """
        root = Path(cfg.projects_root)
        if not root.is_dir():
            return {"ok": False, "cause": "no_projects_root", "error": f"项目根不存在：{root}"}
        rows = []
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue                      # `_incoming` / `_rehearsal` 是内部目录
            raw = d / "input" / "raw.mp4"
            rows.append({
                "名字": d.name,
                "有源片": raw.is_file(),
                "源片MB": round(raw.stat().st_size / 1048576, 1) if raw.is_file() else None,
                "有逐字稿": (d / "edit" / "transcript.json").is_file(),
                "已定时间轴": (d / "edit" / "timeline_map.json").is_file(),
                "有字幕": (d / "edit" / "subtitles" / "final.srt").is_file(),
                "有成片": (d / "renders" / "final.mp4").is_file(),
            })
        cur = ctx.project.name if ctx.project is not None else None
        return {"ok": True, "项目数": len(rows), "当前绑定": cur, "项目": rows}

    def use_project(name: str) -> dict:
        """把当前会话**切到**另一个项目。之后所有工具都作用在那个项目上。

        name 传项目目录名（先 list_projects 看有哪些）。
        ⚠️ 只切"在跟谁打交道"，不改动任何项目里的文件。
        """
        if not name or not str(name).strip():
            return {"ok": False, "cause": "bad_name", "error": "项目名不能为空"}
        try:
            pdir = cfg.resolve_project_dir(Path(cfg.projects_root) / str(name).strip())
        except ValueError as e:
            return {"ok": False, "cause": "bad_project", "error": str(e)}
        if not pdir.is_dir():
            return {"ok": False, "cause": "no_such_project",
                    "error": f"没有这个项目：{name}（用 list_projects 看有哪些）"}
        ctx.project = pdir
        return {"ok": True, "已切到": pdir.name, "目录": str(pdir)}

    def inspect_project() -> dict:
        """看**当前会话绑定的项目**现在长什么样。

        返回：已经有哪些产物、还缺哪些、内核认为下一步该做什么、源片规格。
        这是**开始任何操作前都该先看一眼**的地方 —— 不要凭记忆假设项目进度。
        会话还没绑定项目时会如实说明，并告诉你下一步怎么绑。
        """
        if ctx.project is None:
            return {
                "ok": False, "cause": "no_project", "未绑定项目": True,
                "说明": "这个会话还没有绑定项目，所以没有可看的项目状态。",
                "下一步": ["调 list_projects 看有哪些项目",
                          "调 use_project 绑定一个",
                          "或者请用户上传一个视频（上传后会自动绑定）"],
            }
        r = cli.status(proj)
        rep = r.report or {}
        arts = rep.get("artifacts") or {}
        return {
            "ok": r.ok,
            "项目": proj.name,
            "源片": {k: v for k, v in (rep.get("artifacts") or {}).get(
                "raw_video", {}).items()},
            "已有产物": sorted(k for k, v in arts.items() if (v or {}).get("exists")),
            "缺少产物": sorted(k for k, v in arts.items() if not (v or {}).get("exists")),
            # ⚠️ 这个字段是**标准全流程**的口径，不是"用户这次要做什么"。
            #    加这句注解是被真事教育出来的：主管看到"下一步：2_plan"就照着往下做，
            #    于是用户只要"加字幕"，它却顺手把裁剪也做了。
            "标准全流程的下一步": rep.get("next_stage"),
            "注意": "上面这个只是标准全流程的口径，**不代表用户这次要做的任务**；"
                    "请按用户实际要求挑工具。",
            "上次质检": {"ok": rep.get("last_qc_ok"), "causes": rep.get("last_qc_causes")},
            "源片规格": rep.get("source_duration") and {
                "时长秒": rep.get("source_duration"), "段数": rep.get("segment_count")},
            "动效工程在不在": (rep.get("overlay") or {}).get("index_html_exists"),
            "动效层渲染了几个": len((rep.get("overlay") or {}).get("rendered_movs") or []),
        }

    def read_artifact(which: str) -> dict:
        """读一份中间产物，看它具体写了什么。

        which 取值：transcript（剪前逐字稿，word 级时间戳）/ timeline（剪后时间轴）/
        plan_context（剪辑证据）/ edit_decisions（删留决策）/ cut_blocks（保留块）/
        subtitle_fixes / final_srt（成品字幕）/ mix_spec / assemble_spec /
        overlay_html（动效工程）/ qc_report / pipeline_report。

        大文件只返回前若干条并**注明被截断**（截断一定留痕，不然会误以为"就这么多"）。
        """
        rel = READABLE.get(which)
        if rel is None:
            return {"ok": False, "error": f"不可读的产物名 {which!r}；可选 {sorted(READABLE)}"}
        p = proj / rel
        if not p.is_file():
            return {"ok": False, "cause": "missing_file", "error": f"{rel} 还不存在"}
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        if rel.endswith(".srt") or rel.endswith(".html"):
            return {"ok": True, "path": rel, "内容": text[:4000],
                    **({"截断说明": f"只给了前 4000 字，全文 {len(text)} 字"} if len(text) > 4000 else {})}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": True, "path": rel, "内容": text[:2000]}
        if isinstance(data, list) and len(data) > 40:
            return {"ok": True, "path": rel, "条数": len(data), "前40条": data[:40],
                    "截断说明": f"共 {len(data)} 条，只给前 40 条"}
        for key in ("segments", "blocks", "checks"):
            if isinstance(data, dict) and isinstance(data.get(key), list) and len(data[key]) > 40:
                small = dict(data)
                small[key] = data[key][:40]
                small["截断说明"] = f"{key} 共 {len(data[key])} 条，只给前 40 条"
                return {"ok": True, "path": rel, "内容": small}
        return {"ok": True, "path": rel, "内容": data}

    # ------------------------------------------------------- 素材 → 逐字稿
    def transcribe() -> dict:
        """把源片转写成逐字稿（GPU，含 word 级时间戳）。

        前置：input/raw.mp4 存在。
        产出：edit/transcript.json（原片时间坐标）+ transcript.txt。
        耗时：本机实测 75 秒的素材约 24 秒。
        ⭐ 这是**所有下游的共同素材**（剪辑判断、字幕、动效都要它），只做一次。
           已有逐字稿就别再转 —— 看一眼返回里的 skipped 或先 inspect_project。
        """
        return _ok(cli.transcribe(proj), ctx, S.TRANSCRIBE)

    # -------------------------------------------------------------- 裁剪
    def decide_cuts(instructions: str = "") -> dict:
        """让模型看逐字稿与证据，判断该删哪些段。

        什么时候用它：用户给的是**模糊标准**（"把废话剪掉""删掉重复讲的那几遍"）
        —— 需要模型看着证据自己判断。
        什么时候**别**用它：用户**点名**了具体哪几句（"把第 3 句删掉"）⇒
        用 `set_cut_plan`：那是确定性的，不会多删。

        `instructions`：**把用户的原话原样抄进来**（例如"把开头那两段删掉，后面别动"）。
        ⚠️ 不传的话，模型只能按自己的观感猜 —— 实测后果正是"用户只要加字幕，它却删了 6 段"。
        也可以传负向要求，例如"用户没要求删减，请给空数组"。

        前置：edit/transcript.json 存在（先 transcribe）。
        产出：edit/plan_context.json（证据）+ edit/edit_decisions.json（决策）。
        ⚠️ 这一步**不动视频** —— 只写决策，改主意还能重来（要真剪再调 apply_cut）。
        ⚠️ 删"幻觉嫌疑段"会被内核拒绝（hallucination_cut_forbidden）：那不是 bug，
           是刻意设的红线。
        """
        ev = _ok(cli.plan_cut(proj), ctx, S.PLAN_CUT)
        if not ev.get("ok"):
            return {"阶段": "产出剪辑证据", **ev}
        judged = _run_node_once(ctx, S.PLAN_CUT,
                                goal_extra={"user_request": str(instructions or "").strip()})
        return {"阶段": "判断删留",
                "用户要求": str(instructions or "").strip() or "（没给 —— 模型按证据自行判断）",
                "证据阶段": ev, "结果": judged}

    def apply_cut() -> dict:
        """按 edit_decisions.json 真的剪切，并自检接缝。

        前置：edit/edit_decisions.json 存在（先 decide_cuts）。
        产出：renders/edited_final.mp4（剪后视频）+ edit/timeline_map.json（剪后时间轴）。
        ⚠️ 会改变时间轴（剪前那份此后不能用于动效对齐）。
        耗时：75 秒素材实测约 37 秒。

        ⚠️ 若用户**没有要求删减内容**，别用这个 —— 用 `establish_timeline`
           （它不删任何东西，只把时间轴确定下来）。
        """
        return _ok(cli.cut(proj, run=True), ctx, S.APPLY_CUT)

    def establish_timeline() -> dict:
        """**不删任何内容**，只把剪辑时间轴确定下来，并产出一份视频副本。

        什么时候用它：字幕要**烧录**，就得有底片（`renders/edited_final.mp4`）；
        而烧录前时间轴也要定下来 —— 这一步一次把两件事都办了。
        什么时候不要用它：用户明确要裁剪（"剪掉废话""去掉开头"）—— 那用 decide_cuts。

        产出：edit/cut_blocks.json（覆盖全片的块）+ edit/timeline_map.json
              + renders/edited_final.mp4（内容与原片一致）。
        耗时：秒级（不删东西时是流拷贝，不需要重编码）。
        ⛔ 它会写 edit_decisions.json（空删除列表）。这一步**不做任何删减判断**，
           所以不会替用户删掉任何内容 —— 那正是它存在的理由。

        ⚠️ **动效不需要这个**（它只要时间戳，`design_overlay` 自己会补时间轴）——
           别为了加动效去跑这一步、白产一份视频副本。
        """
        write_json(ctx.project / "edit" / "edit_decisions.json",
                   _zero_drop_decisions(ctx.project))
        return _ok(cli.cut(proj, run=True), ctx, S.APPLY_CUT)

    # ========================== 改内容（不是"跑阶段"）==========================
    # 这一组和上面的工具性质不同：它们**直接改中间产物**。
    # 为什么必须有：用户说"把第 3 句删掉"时，光会"跑阶段"是不够的 ——
    # 得能改那份决策文件、再触发续跑。但改动必须落在**内核支持的确定性操作**范围内
    # （删段 / 改字幕文本）；内核做不到的（"语气改热情点"）只能如实说做不到。
    def _suspect_set() -> set[int]:
        """哪些 idx 是**幻觉嫌疑段**（删了会被内核拒）。

        优先读 `edit/plan_context.json` 里内核已经算好的标记 —— 那正是模型当初看到的那份，
        口径完全一致。没有它就用内核那个函数现算（**同一套规则，不另写一遍**）。
        """
        ctxp = proj / "edit" / "plan_context.json"
        if ctxp.is_file():
            try:
                segs = (json.loads(ctxp.read_text(encoding="utf-8-sig")) or {}).get("segments") or []
                if segs:
                    return {int(s["idx"]) for s in segs if s.get("suspect_hallucination")}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        try:
            from pipeline.common import is_suspect_hallucination  # 内核的那个函数

            segs = json.loads((proj / "edit" / "transcript.json").read_text(encoding="utf-8-sig"))
            return {i for i, s in enumerate(segs) if is_suspect_hallucination(s)}
        except Exception:  # noqa: BLE001 —— 算不出来就不预检，内核那道断言仍然在
            return set()

    def _stale_after_recut() -> list[str]:
        """重剪之后**必须重跑**的下游产物（只列盘上真有的，不吓唬人）。"""
        items = [
            ("edit/subtitles/final.srt", "字幕档"),
            ("renders/subtitle_test_10s.mp4", "字幕测试片"),
            ("renders/subtitled_full.mp4", "全片字幕版"),
            ("overlays/index.html", "动效工程（按旧时间轴写的）"),
            ("overlays/renders/overlay.mov", "已渲染的动效层"),
            ("renders/final.mp4", "成片"),
            ("edit/qc_report.json", "质检报告"),
        ]
        return [f"{rel}（{label}）" for rel, label in items if (proj / rel).exists()]

    def list_segments() -> dict:
        """把逐字稿按**段**列出来 —— 用来确定"第几句"到底指哪一段。

        ⚠️ 两套编号，**差一**，别混：
           · `第N句` = 人话里的第几句，**从 1 开始**；
           · `idx`   = 内核删除集用的下标，**从 0 开始**。恒有 `idx = N - 1`。
        用户说"第 3 句" ⇒ 指的是 `第3句`，对应 `idx=2`。

        返回：idx / 第N句 / 起止秒 / 时长 / 字数 / 文本（截到 60 字）/ 幻觉嫌疑标记。
        要改内容（删段、改字幕）之前先看它，别凭印象说"第 3 句"。
        """
        p = proj / "edit" / "transcript.json"
        if not p.is_file():
            return {"ok": False, "cause": "missing_file",
                    "error": "edit/transcript.json 还不存在 —— 先 transcribe。"}
        try:
            segs = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return {"ok": False, "cause": "bad_transcript", "error": f"读不动逐字稿：{e}"}
        if not isinstance(segs, list):
            return {"ok": False, "cause": "bad_transcript", "error": "transcript.json 不是数组"}
        susp = _suspect_set()
        rows = []
        for i, s in enumerate(segs):
            try:
                a, b = float(s.get("start") or 0), float(s.get("end") or 0)
            except (TypeError, ValueError):
                a, b = 0.0, 0.0
            txt = str(s.get("text") or "").strip()
            rows.append({
                "idx": i, "第N句": i + 1,
                "start": round(a, 2), "end": round(b, 2), "dur": round(b - a, 2),
                "chars": len(txt),
                "text": txt[:60] + ("…" if len(txt) > 60 else ""),
                "幻觉嫌疑": (i in susp) if susp else None,
            })
        return {"ok": True, "段数": len(rows),
                "编号说明": "`第N句` 从 1 开始（人话口径）；`idx` 从 0 开始（内核删除集口径）；idx = N-1",
                "段": rows}

    def set_cut_plan(drop_idx: list[int], reason: str) -> dict:
        """**确定性地**指定从时间轴上删掉哪几段（不调模型、不剪辑）。

        什么时候用它：
          · 用户**点名**要删（"把第 3 句删掉""去掉开头两段"）—— 你说删哪段就删哪段；
          · 用户**反悔**了（"全都别删了"）—— 传空数组即可恢复全片。
        什么时候**别**用它：用户只给了模糊标准（"把废话剪掉"）⇒ 用 decide_cuts，
          让模型看着证据自己判断。

        `drop_idx`：要删的 0-based 段下标（见 list_segments 的 idx）。**替换语义**，
          传进来就是完整的删除集，不是往上追加。
        `reason`：**必填**，写清为什么删（会记进决策文件，供日后追溯）。

        ⚠️ 只写决策，**不动视频**。要真剪还得再调 apply_cut。
        ⚠️ 传"幻觉嫌疑段"会被拒（内核红线，返回里会说清是哪几段、为什么）。
        """
        p = proj / "edit" / "transcript.json"
        if not p.is_file():
            return {"ok": False, "cause": "missing_file", "error": "还没有逐字稿 —— 先 transcribe。"}
        try:
            segs = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return {"ok": False, "cause": "bad_transcript", "error": f"读不动逐字稿：{e}"}
        n = len(segs)

        why = str(reason or "").strip()
        if len(why) < 4:
            return {"ok": False, "cause": "bad_reason",
                    "error": "reason 要写清为什么删（至少 4 个字）—— 删减是一次要有据可查的决定。"}
        try:
            drop = sorted({int(x) for x in (drop_idx or [])})
        except (TypeError, ValueError):
            return {"ok": False, "cause": "bad_spec",
                    "error": f"drop_idx 必须是整数数组，实际 {drop_idx!r}"}
        bad = [d for d in drop if d < 0 or d >= n]
        if bad:
            return {"ok": False, "cause": "index_out_of_range",
                    "error": f"这些下标越界：{bad}（合法 0..{n-1}）。"
                             f"注意 `第N句` 要**减 1** 才是 idx —— 第 3 句 ⇒ idx=2。"}
        if len(drop) >= n:
            return {"ok": False, "cause": "empty_keep_set", "error": "不能把全部段都删掉。"}

        hits = [d for d in drop if d in _suspect_set()]
        if hits:
            return {"ok": False, "cause": "hallucination_cut_forbidden",
                    "error": f"要删的段里有 {len(hits)} 个**幻觉嫌疑段**：{hits}",
                    "说明": "它们时长 ≤0.5s 却 ≥6 字 —— 音频上其实是一整句连贯的话，"
                            "删时间轴会把说话声切断，所以内核不许删（这些段只在字幕层合并）。",
                    "怎么办": ["把它们从 drop_idx 里去掉（多半是序号算错了）",
                              "若用户坚持要删，如实告诉他这是内核红线、删不了"],
                    "涉及": [{"idx": d, "第N句": d + 1,
                              "text": str(segs[d].get("text") or "")[:40]} for d in hits]}

        # ⚠️ 保留原有的 text_fixes：文本修正与删除集是两码事，整份覆盖会把它一起抹掉。
        dp = proj / "edit" / "edit_decisions.json"
        prev_fixes = {}
        if dp.is_file():
            try:
                prev = json.loads(dp.read_text(encoding="utf-8-sig")) or {}
                dropped = {str(d) for d in drop}
                prev_fixes = {k: v for k, v in (prev.get("text_fixes") or {}).items()
                              if str(k) not in dropped}
            except (OSError, ValueError):
                prev_fixes = {}
        write_json(dp, {"source": "input/raw.mp4",
                        "authored_by": f"主管：按用户点名定删（{why}）",
                        "drop_segment_ids": drop,
                        "text_fixes": prev_fixes,
                        "notes": why})
        out = {"ok": True, "已写": "edit/edit_decisions.json",
               "将删": [{"idx": d, "第N句": d + 1,
                         "text": str(segs[d].get("text") or "")[:50]} for d in drop],
               "将保留": n - len(drop), "原片段数": n,
               "下一步": "调 apply_cut 才会真的剪（那一步要重编码，分钟级）。"}
        stale = _stale_after_recut()
        if stale:
            out["⚠️重剪后必须重跑"] = stale
            out["说明_作废"] = ("它们都是按**旧的保留块**算出来的，重剪之后内容就对不上了；"
                                "请如实告诉用户哪些要重跑，别让他以为成片还是新的。")
        return out

    # -------------------------------------------------------------- 字幕
    def make_subtitles() -> dict:
        """生成字幕档 edit/subtitles/final.srt。

        ⚠️ **前置：edit/cut_blocks.json 必须存在** —— 字幕的每一行时间都是按
           "保留块"从原片时间轴**重映射**出来的，没有保留块就无从下手。
           若项目还没有它：用户要删减 ⇒ 先 decide_cuts + apply_cut；
                          用户没要删减 ⇒ 先用 establish_timeline（不删任何内容）。
        产出：final.srt。纯确定性重映射，不调模型、秒级。
        """
        if not (proj / "edit" / "cut_blocks.json").is_file():
            return {
                "ok": False, "cause": "missing_prerequisite",
                "缺前置": "edit/cut_blocks.json",
                "说明": ("字幕是按「保留块」重映射出来的，所以先要有剪辑时间轴。"
                         "用户没要求删减的话，请先用 establish_timeline（零删除）建立时间轴；"
                         "用户要裁剪的话，用 decide_cuts + apply_cut。"),
            }
        return _ok(cli.srt(proj), ctx, S.BUILD_SRT)

    def fix_subtitle_text() -> dict:
        """让模型按逐字稿纠正字幕文本（错别字、数字写法、标点）。

        前置：final.srt 或 transcript 存在。
        产出：edit/subtitle_fixes.json（只改文本，不动时间轴）。
        ✅ 你**点名改过**的条目（`manual`）**不会被它覆盖** —— 人的意图优先于模型纠错。
        """
        return _run_node_once(ctx, S.PLAN_SUBTITLES)

    def set_subtitle_text(segment_idx: int, text: str) -> dict:
        """**点名**改某一句话的字幕文本（错别字、数字读法、标点）。

        `segment_idx`：**0-based 段下标**（就是 list_segments 里的 `idx`）。
          用户说"第 3 句" ⇒ 传 2（**要减 1**）。
        `text`：改成什么。传**空字符串** ⇒ **撤销**这条修正、恢复该句原始文本。

        产出：edit/subtitle_fixes.json 里的 `manual` 段。
        ✅ `manual` 是**人的意图**：`fix_subtitle_text`（模型整片纠错）跑完不会覆盖它 ——
           所以"先手动改一句、再让它整片纠一遍"，你的改动还在。
        ⚠️ 只改文本、**不动时间轴**，也不会自动重排字幕行。
        ⚠️ 改完 final.srt 还是旧的 —— 要生效得再调 make_subtitles；已烧录的片子要重烧。
        """
        tp = proj / "edit" / "transcript.json"
        if not tp.is_file():
            return {"ok": False, "cause": "missing_file", "error": "还没有逐字稿 —— 先 transcribe。"}
        try:
            segs = json.loads(tp.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return {"ok": False, "cause": "bad_transcript", "error": f"读不动逐字稿：{e}"}
        n = len(segs)
        try:
            i = int(segment_idx)
        except (TypeError, ValueError):
            return {"ok": False, "cause": "bad_spec",
                    "error": f"segment_idx 必须是整数，实际 {segment_idx!r}"}
        if not (0 <= i < n):
            return {"ok": False, "cause": "index_out_of_range",
                    "error": f"越界：{i}，合法 0..{n - 1}。`第N句` 要**减 1** 才是 idx。"}

        sp = proj / "edit" / "subtitle_fixes.json"
        data = {}
        if sp.is_file():
            try:
                data = json.loads(sp.read_text(encoding="utf-8-sig")) or {}
            except (OSError, ValueError):
                data = {}
        manual = {str(k): str(v) for k, v in (data.get("manual") or {}).items()}
        # ⚠️ `auto` 必须取"text_fixes 里**不属于 manual** 的那部分"。
        #    不能直接拿 text_fixes 当 auto —— 它是"auto ∪ manual"的**合并结果**，
        #    那样撤销一条人工修正时，旧值会被残留的 text_fixes 带回来（实测踩过：
        #    撤销后 manual 空了，text_fixes 里那条还在 ⇒ 内核照样按它改，撤销等于没撤）。
        auto = {k: v for k, v in (data.get("text_fixes") or {}).items() if str(k) not in manual}
        new = str(text or "").strip()
        if new:
            manual[str(i)] = new
        else:
            manual.pop(str(i), None)
        write_json(sp, {"source": "user:点名改字幕",
                        "text_fixes": {**auto, **manual},
                        "manual": manual})
        return {"ok": True, "已写": "edit/subtitle_fixes.json",
                "idx": i, "第N句": i + 1,
                "原文": str(segs[i].get("text") or "")[:80],
                "现在改成": new or "（已撤销这条修正，恢复原文）",
                "人工修正共几条": len(manual),
                "下一步": "调 make_subtitles 重算 final.srt；已烧录的片子要重烧才看得到。"}

    def burn_subtitles(mode: str) -> dict:
        """把字幕烧进画面。

        mode="test"：只烧前 10 秒并抓帧，用来验排版（**便宜，约 2 秒**）。
        mode="full"：烧全片。⚠️ 内核有护栏：**没有测试片 + 抓帧证据会被拒**，
                     所以要么先跑 mode="test"，要么先看一眼测试片。
        前置：edit/subtitles/final.srt 存在。
        产出：renders/subtitle_test_10s.mp4 或 renders/subtitled_full.mp4。
        """
        # 节点名随 mode 变：审片那一步是靠 BURN_TEST 的结果去找抓帧证据的，
        # 记错名字它就只能掉进不可信的兜底扫描。
        return _ok(cli.burn(proj, mode=mode), ctx,
                   S.BURN_TEST if mode == "test" else S.BURN_FULL)

    def review_test_burn() -> dict:
        """看图复核测试片：字幕位置、可读性、有没有挡住脸。

        前置：renders/subtitle_test_10s.mp4 存在。
        ⚠️ 这一步只能**给结论和建议**，不修改任何产物。
        如果结论是"字幕挡脸"，唯一的可调项是 MarginV（离底边距离，越大越往上），
        之后用 burn_subtitles 重烧时带上它。
        """
        return _run_node_once(ctx, S.REVIEW_TEST)

    # -------------------------------------------------------------- 动效
    def design_overlay() -> dict:
        """设计动效工程：让模型按时间轴写 overlays/index.html。

        前置：edit/transcript.json（**逐字稿里有时间戳就够了**）。
              ⚠️ **不需要先裁剪** —— 没剪过片时，逐字稿的原始秒数就是成片秒数；
              项目若还没有时间轴，这里会按「零删除」自动补一份（不产视频、不动画面）。
              若项目**剪过**（已有 edit/timeline_map.json），一律用它 —— 那份才是正确坐标。
        产出：overlays/index.html（透明叠加层工程，GSAP 时间轴）。
        ⚠️ 只写工程、**不渲染**。渲染是分钟级的，设计是秒级的 —— 先设计、确认后再渲染。
        ⚠️ 本机未装 HyperFrames 的话，渲染那步会失败（要在项目 overlays/ 下建
           node_modules 联接，见项目文档）。
        """
        ready, note = _ensure_timeline(ctx)
        if not ready:
            return {"ok": False, "cause": "missing_prerequisite", "说明": note}
        out = _run_node_once(ctx, S.DESIGN_OVERLAY)
        if note and out.get("ok"):
            out["时间轴来源"] = note
        return out

    def render_overlay() -> dict:
        """把动效工程渲染成带 alpha 的透明层（ProRes4444 的 .mov）。

        前置：overlays/index.html 存在，且工程能过 hyperframes lint。
        产出：overlays/renders/*.mov（**很大，1080p 一分钟约 150 MB**）。
        耗时：实测约 132 秒。
        ⚠️ lint 不过会以 hyperframes_lint_failed 拒绝。返回里会带具体的 Fix 说明 ——
           照着改 index.html 再来一次，别硬重试同一份工程。
        """
        return _ok(cli.overlay(proj), ctx, S.RENDER_OVERLAY)

    # --------------------------------------------------------- 合成 / 成片
    def prepare_final_specs(style: str = "") -> dict:
        """写成片链路的三份规格：edit/bgm.json、edit/mix.json、edit/assemble.json。

        前置：edit/timeline_map.json 或 transcript 存在。
        参数 style：BGM 风格描述（中文一句话）。留空则由模型按片子内容自己定。
        产出：三份 JSON。**这一步不渲染、不花钱**，纯写规格。
        ⚠️ 规格里的文件路径由代码写死，不由模型决定 —— 这是管道一致性，不是创意。
        """
        return _run_node_once(ctx, S.PLAN_FINAL,
                              {"bgm_style": style} if style else None)

    def generate_bgm(confirm_paid: bool = False) -> dict:
        """生成背景音乐（**唯一会花钱的一步**，约 0.49 元/次）。

        前置：edit/bgm.json 存在（先 prepare_final_specs）。
        行为：
          · 已有可用成品 ⇒ **免费复用**，不会重复扣费（这是最常走到的分支）；
          · 没有成品且未获授权 ⇒ 返回 needs_authorization + 价格，**绝不提交**；
          · 已获用户授权 ⇒ 才真提交。

        ⛔ 参数 confirm_paid 只能表达"我知道这要花钱"，**不能代替用户授权** ——
        真正的开关是用户在界面上点的那个按钮（服务端会话里的授权位，agent 改不了）。
        """
        probe = cli.bgm(proj, confirm_paid=False)
        if probe.ok or probe.cause == "bgm_exists":
            out = _ok(probe, ctx, S.GENERATE_BGM)
            out["说明"] = "已有可用成品，免费复用，未重复扣费"
            return out
        if probe.cause != "bgm_needs_confirm":
            return _ok(probe, ctx, S.GENERATE_BGM)   # 别的失败交给主管判读

        quote = cli.bgm(proj, dry_run=True)          # 免费查价
        price = (quote.report or {}).get("price_per_call")
        if not ctx.paid_ok:
            return {
                "ok": False, "cause": "needs_authorization",
                "需要用户授权": True, "价格元每次": price,
                "说明": ("生成 BGM 要花钱，而用户还没有授权。请把价格告诉用户，"
                         "请他在界面上点「授权本次付费」；**不要**自己判断可以花。"),
            }
        if not confirm_paid:
            return {
                "ok": False, "cause": "confirm_flag_missing",
                "说明": "用户已授权，但你没有带 confirm_paid=True。请带上它再调一次。",
            }
        result = _ok(cli.bgm(proj, confirm_paid=True), ctx, S.GENERATE_BGM)
        # ⛔ 授权**一次一用**：真提交过就立刻收回。否则主管在同一轮里
        #    因为别的原因再调一次，就会在用户不知情的情况下重复扣费。
        ctx.paid_ok = False
        return result

    def mix_audio(mode: str) -> dict:
        """合成音频/成片主轨。

        mode="assemble"：xfade 缝接保留块 → renders/skeleton.mp4（骨架，保留人声）。
        mode="mix"：把 BGM 铺进人声主轨 → renders/assembled.mp4（**无特效版成片**）。
        前置：mode="assemble" 需 edit/assemble.json；mode="mix" 需 edit/mix.json
              （都来自 prepare_final_specs），且 BGM 文件已在 bgm/ 下。
        耗时：几十秒。
        """
        spec = "edit/assemble.json" if mode == "assemble" else "edit/mix.json"
        return _ok(cli.assemble(proj, mode=mode, spec=spec), ctx,
                   S.ASSEMBLE_BASE if mode == "assemble" else S.MIX_BGM)

    def apply_overlay() -> dict:
        """把动效层叠到成片上 → **唯一的交付物** renders/final.mp4。

        前置：renders/assembled.mp4（先 mix_audio）+ overlays/renders/*.mov（先 render_overlay）。
        产出：renders/final.mp4。
        耗时：几十秒。
        """
        # ⚠️ 前置要**新鲜**，不只是"存在"。
        #    这条检查是并发放开之后必须加的：`apply_overlay` 要垫在 `assembled.mp4` 上，
        #    而那个文件是从"烧完字幕的片子"一路缝接+混音出来的。如果它比 `timeline_map.json`
        #    （剪裁的产物）还旧，就说明它是**上一轮**留下的底片 —— 拿它叠出来的成片
        #    内容对不上，而且**不会报错**。
        #    本项目最忌讳的就是这种"看着成功、其实是错的"，所以宁可直接拒绝。
        base = proj / "renders" / "assembled.mp4"
        cut = proj / "edit" / "timeline_map.json"
        if not base.is_file():
            return {"ok": False, "cause": "missing_input",
                    "说明": "还没有 renders/assembled.mp4（要先 mix_audio 缝接+混音）。"}
        if cut.is_file() and base.stat().st_mtime < cut.stat().st_mtime:
            return {"ok": False, "cause": "stale_input",
                    "说明": ("renders/assembled.mp4 比剪辑时间轴还旧 —— 它是**上一轮**留下的底片，"
                             "拿它叠出来的成片内容对不上。请先重跑 混音（mix_audio）再叠动效。")}
        return _run_node_once(ctx, S.APPLY_OVERLAY)

    # -------------------------------------------------------------- 质检
    def run_qc(target: str) -> dict:
        """对某个产物跑数字断言（含抓帧取证）。

        target 取值决定**检查项集合**，选错会拿到假报警：
          · "subtitle_test"：10 秒测试片（查字幕排版/时长）
          · "edited"：剪后视频（查剪点/时长）
          · "subtitled"：烧字全片
          · "final"：成片（查音画、BGM 有没有真进去、峰值、黑帧…）
        返回里 checks_failed 会列出没过的那几条。**这一步只断言、不修**。
        """
        return _ok(cli.qc(proj, target=target), ctx, S.RUN_QC)

    # --------------------------------------------------------- 标准全流程
    def run_standard_pipeline() -> dict:
        """按**标准全流程**跑一遍：转写 → 裁剪 → 字幕 → 烧录 → 动效 → 混音 → 质检 → 交付。

        什么时候用它：用户说"按标准流程来一遍""完整出一版"，或者你判断**各阶段都要做**
        且顺序就是标准顺序。
        什么时候**不要**用它：用户只要其中一两件事（"帮我加字幕"）——
        那时请挑对应的细粒度工具，跑全流程会白白多做几个阶段（转写和全片编码都是分钟级）。
        前提：必须已有项目（源片在 input/raw.mp4）。这会花较长时间，
        花钱的动作（BGM）仍会停下来等用户授权。
        """
        # 交给 webapp 的 run 通道执行（它已有流式、账本、中断那套）
        return {
            "ok": True,
            "交给界面执行": True,
            "动作": "run_standard_pipeline",
            "说明": ("标准全流程需要在界面上跑（有实时进度与付费中断卡片）。"
                     "我已经把这条请求转成一次标准运行，进度请看左侧流水线面板。"),
        }

    # ------------------------------------------------------- 知识包（skill）
    def list_skills() -> dict:
        """列出本项目里可用的**知识包**（skill）：规范、阈值、规则全集这类参考资料。

        什么时候用它：**动手前先扫一眼**，看你这次要做的事有没有对应的规范。
        尤其是排版 / 渲染契约类的硬规矩 —— 写错了往往渲染才失败（或产出静止画面），
        而那时已经烧掉几分钟。
        返回：每个知识包的 `name`（`load_skill` 要用它）、适用角色、一行摘要。
        ⚠️ **只有目录，没有正文** —— 正文按需用 `load_skill` 取，别一次全读。
        """
        items = _all_skills(ctx)
        if not items:
            return {"ok": True, "知识包": [], "说明": "没有任何知识包 —— 按通用做法来。"}
        return {
            "ok": True,
            "知识包": [{"name": s["name"], "title": s["title"],
                        "适用于": s["applies_to"], "摘要": s["summary"]} for s in items],
            "用法": "觉得相关就用 load_skill(name) 读全文；不相关就别读（读进来会占上下文）。",
        }

    def load_skill(name: str) -> dict:
        """读某个知识包的**全文**。

        参数 name：`list_skills` 给出的名字，**照抄**（别自己拼、别猜）。
        ⚠️ 名字不存在时会返回可用清单 —— 照它改，不要连试几次。
        """
        want = str(name or "").strip()
        items = _all_skills(ctx)
        hit = next((s for s in items if s["name"] == want or s["title"] == want), None)
        if hit is None:
            return {"ok": False, "cause": "skill_not_found",
                    "说明": f"没有叫 {want!r} 的知识包。",
                    "可用的": [s["name"] for s in items] or "（这个项目下没有知识包）"}
        return {"ok": True, "name": hit["name"], "title": hit["title"],
                "适用于": hit["applies_to"], "正文": hit["body"]}

    specs: list[tuple[str, Any, str]] = [
        ("list_skills", list_skills,
         "列出本项目的知识包（规范/阈值/规则全集）；动手前先扫一眼有没有相关规范"),

        ("load_skill", load_skill,
         "读取某个知识包的全文（名字照抄 list_skills 给的，别自己拼）"),

        ("list_projects", list_projects,
         "列出所有项目及各自做到哪一步（会话没绑定项目、或不确定用户指哪个时先看这个）"),

        ("use_project", use_project,
         "把当前会话切到另一个项目上（只切工作对象，不动文件）"),

        ("inspect_project", inspect_project,
         "看当前项目现在长什么样：有哪些产物、缺哪些、内核认为下一步做什么"),

        ("read_artifact", read_artifact,
         "读一份中间产物的具体内容（逐字稿/时间轴/决策/字幕/规格/质检报告等）"),

        ("transcribe", transcribe,
         "把源片转写成逐字稿（GPU，含 word 级时间戳）。后面几乎所有步骤的共同前置"),

        ("decide_cuts", decide_cuts,
         "让模型按证据判断该删哪些段（instructions 里放用户原话；只写决策，不动视频）"),

        ("list_segments", list_segments,
         "列出逐字稿的每一段（含「第N句」与 0-based idx 两套编号）—— 改内容前先看它"),

        ("set_cut_plan", set_cut_plan,
         "【改内容】点名指定删哪几段（替换语义；只写决策不剪；反悔传空数组；必填 reason）"),

        ("apply_cut", apply_cut,
         "按决策真的剪切，产出剪后视频与剪后时间轴"),

        ("establish_timeline", establish_timeline,
         "不删任何内容，只把剪辑时间轴确定下来（给字幕/动效打地基，用户没要求删减时用这个）"),

        ("make_subtitles", make_subtitles,
         "生成字幕档 final.srt（纯确定性重映射，不调模型）"),

        ("fix_subtitle_text", fix_subtitle_text,
         "让模型按逐字稿纠正字幕文本（错别字/数字/标点），不动时间轴；不覆盖人工改过的"),

        ("set_subtitle_text", set_subtitle_text,
         "【改内容】点名改一句字幕的文本（0-based 段下标；传空文本=撤销；不动时间轴）"),

        ("burn_subtitles", burn_subtitles,
         "把字幕烧进画面：mode=test 只烧前 10 秒验排版；mode=full 烧全片"),

        ("review_test_burn", review_test_burn,
         "看图复核测试片：字幕位置、可读性、是否挡脸（只给结论，不改产物）"),

        ("design_overlay", design_overlay,
         "设计动效工程 overlays/index.html（按剪后时间轴；只写工程不渲染）"),

        ("render_overlay", render_overlay,
         "把动效工程渲染成带 alpha 的透明层 .mov（分钟级，产物很大）"),

        ("prepare_final_specs", prepare_final_specs,
         "写成片规格：bgm.json / mix.json / assemble.json（不渲染、不花钱）"),

        ("generate_bgm", generate_bgm,
         "生成背景音乐（唯一花钱的一步；已有成品会免费复用；未授权不提交）"),

        ("mix_audio", mix_audio,
         "合成：mode=assemble 缝接骨架；mode=mix 把 BGM 铺进人声主轨出无特效版成片"),

        ("apply_overlay", apply_overlay,
         "叠加动效层 → 唯一交付物 renders/final.mp4"),

        ("run_qc", run_qc,
         "对某个产物跑数字断言与抓帧取证（target 选错会有假报警）"),

        ("run_standard_pipeline", run_standard_pipeline,
         "按标准全流程跑一整遍（只在用户明确要「完整出一版」时用）"),
    ]

    unknown = (only or set()) - {n for n, _f, _d in specs}
    if unknown:
        # 拼错的名字会被**静默丢掉**，于是子代理少了一个工具却不知道自己少了 ——
        # 那种故障的表现是"它说做不到"，而根因在配置里。所以这里直接炸。
        raise KeyError(f"白名单里有不存在的工具名：{sorted(unknown)}")

    return [
        StructuredTool.from_function(func=fn, name=name, description=desc)
        for name, fn, desc in specs
        if only is None or name in only
    ]
