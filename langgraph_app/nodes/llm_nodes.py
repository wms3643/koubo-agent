"""判断节点 —— 四个专业角色的 LLM 判断，共 6 个节点。

分工（与契约 §五 一致）：

    剪裁策划  plan_cut        哪几段该从时间轴消失（幻觉段绝不许删）
    字幕制作  plan_subtitles  纯文本纠错 + 跨删除区句子的取舍
    特效设计  design_overlay  按**剪后时间轴**设计动效工程
    剪裁策划  plan_final      成片规格三件套（bgm.json / mix.json / assemble.json）
    成片质检  review_test     看测试片抓帧（CP2：字幕真烧进、不挡脸）
    成片质检  review_final    看全片抓帧 + 读数字断言（CP3：能否交付）

统一形状：**读证据 → 让模型出判断 → 由编排层写文件 → 记账**。
模型不直接写文件、也不调工具 —— "判断与执行分离"（`agents/director.md` 原则一）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import routing as R
from .. import stages as S
from ..config import AppConfig
from ..kernel import KernelClient, KernelResult
from ..llm import LLMError, ask, build_chat, extract_html, parse_json
from ..personas import system_prompt_for_node
from ..state import PipelineState
from .base import NodeFn, command, project_of, synthetic, write_json
from .prompts import evidence_block, feedback_block
from .specs import write_final_specs

# 审片节点要重建 Command（见 `_review_node`），所以直接引入类型
from langgraph.types import Command

# 动效工程用"标记 + 原文"交付，而不是塞进 JSON 字符串 —— 理由见 llm.extract_html()
_HTML_MARKER = "<!--OVERLAY_HTML-->"

# 仓库内置的动画库（GSAP 3.12.5，MIT）。见 `_ensure_vendor_js()` 说明为何由代码保证。
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor"
VENDOR_JS = "gsap.min.js"


def _ensure_vendor_js(project: Path) -> str | None:
    """保证 `overlays/vendor/gsap.min.js` 在位，返回一句可记入 notes 的说明（或 None）。

    ⚠️ 为什么必须由代码保证、而不是"让 agent 自己装"：
    提示词第 7 条要求工程引用本地 `./vendor/gsap.min.js`（渲染不该依赖外网），
    但**模型没法装包**（它连工具都不能调）。提示词承诺的东西必须由代码落实，
    否则结果只有两种：渲染时 `gsap is not defined`（产出静止画面，而且不报错），
    或者模型自己改回 CDN（又把渲染押在外网可达性上）。

    查找顺序：项目里已有 → 项目自己的 node_modules → **仓库内置副本**。
    """
    dest = project / "overlays" / "vendor" / VENDOR_JS
    if dest.is_file() and dest.stat().st_size > 0:
        return None
    for src in (
        project / "overlays" / "node_modules" / "gsap" / "dist" / VENDOR_JS,
        VENDOR_DIR / VENDOR_JS,
    ):
        if src.is_file() and src.stat().st_size > 0:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            return f"已放置本地动画库 overlays/vendor/{VENDOR_JS}"
    return None


# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------
def _read_json(path: Path, default=None):
    if not Path(path).is_file():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _finish(node: str, state: PipelineState, config: AppConfig, result: KernelResult):
    goto, update = R.advance(node=node, result=result, state=state, config=config)
    return command(goto, update)


def _llm_fail(node: str, state: PipelineState, config: AppConfig, exc: Exception,
              *, bad_output: bool):
    """模型侧失败 → 翻成可路由 cause（而不是让它冒成异常）。

    `llm_unavailable`（缺密钥/网络）与 `llm_bad_output`（输出不合契约）必须分开：
    前者重试必然失败（会白烧钱），后者重问一次通常就好。混成一个就会做出错误的重试决策。
    """
    cause = "llm_bad_output" if bad_output else "llm_unavailable"
    return _finish(node, state, config, synthetic(
        node, ok=False, cause=cause,
        report={"ok": False, "cause": cause, "error": str(exc)[:600]},
    ))


def _bad_output(node: str, state: PipelineState, config: AppConfig, message: str):
    """模型输出**不合契约**（缺字段 / 越界 / 类型不对）⇒ 可路由的 `llm_bad_output`。

    ⚠️ 为什么要在本地先检查一遍，而不是全丢给内核：内核当然会拦
    （`index_out_of_range` / `bad_spec` 等），但那要多烧一次内核调用，
    而且回喂的信息不如这里精准 —— 全段表就在我们手上，能直接告诉它合法范围是多少。
    **内核那道断言仍然保留**：这是"更早、更清楚"，不是"替代闸门"。
    """
    return _finish(node, state, config, synthetic(
        node, ok=False, cause="llm_bad_output",
        report={"ok": False, "cause": "llm_bad_output", "error": message}))


def _segments(project: Path) -> list[dict]:
    data = _read_json(project / "edit" / "transcript.json", [])
    if isinstance(data, dict):
        data = data.get("segments") or []
    return data or []


def _kept_index_texts(project: Path) -> list[tuple[int, str]]:
    """**保留段**的 `(0-based 原片下标, 文本)` 列表 —— 按剪后顺序。

    为什么要有这个函数：`text_fixes` 的键是**原片下标**，而模型看到的是"保留下来的句子"。
    不给出"保留段 → 原片下标"的对应，模型就只能猜下标 —— 猜错会被内核以
    `index_out_of_range`/改错句子 反噬，且很难查。
    """
    segs = _segments(project)
    blocks = _read_json(project / "edit" / "cut_blocks.json", []) or []
    out: list[tuple[int, str]] = []
    for b in blocks:
        # ⚠️ cut_blocks 的 first_seg/last_seg 是 **1-based**（全项目唯一允许 1-based 的地方）
        for i in range(int(b.get("first_seg", 1)) - 1, int(b.get("last_seg", 0))):
            if 0 <= i < len(segs):
                out.append((i, str(segs[i].get("text") or "").strip()))
    return out


def _review_cause_menu() -> str:
    """给审片子代理的"可选 cause 菜单" —— **从回退矩阵生成**，不手写第二份。"""
    lines = []
    for cause in REVIEW_ALLOWED:
        route = R.MATRIX.get(cause)
        if route is not None:
            lines.append(f"  · {cause} —— {route.note}")
    return "\n".join(lines)


REVIEW_ALLOWED: tuple[str, ...] = (
    "subtitle_occlusion", "srt_bom", "srt_trailing_symbol", "srt_timeline_overlap",
    "srt_timeline_mismatch", "junction_glitch", "audio_clipping", "av_desync",
    "cut_duration_mismatch", "srt_empty", "black_frame",
    "missing_overlay", "hyperframes_render_failed",
    "bgm_silent", "bgm_overpowers_voice", "bgm_true_peak", "bgm_abrupt_cut",
)

_VERDICT_CONTRACT = """\
【回复格式（只输出这一个 JSON 对象，不要围栏、不要解释）】

{
  "pass": true | false,          // 能否进入下一步
  "cause": "<若 pass=false，从下面的菜单里选**一个**>",
  "target": "<可选：若菜单里没有合适的，直接指定回退节点名>",
  "margin_v": <可选数字：字幕挡脸时给新的 MarginV>,
  "reasons": ["<判据，逐条写清你**在图里看到了什么**>"]
}

⛔ 判 pass=true 必须有正面证据（字幕确实烧进画面、没有遮挡、接缝没有跳帧），
   不能因为"看不出问题"就给通过 —— "没证据"与"有问题"在质检里同义。
"""


# ===========================================================================
#  剪裁策划 · 删留判断
# ===========================================================================
def make_plan_cut(client: KernelClient, config: AppConfig) -> NodeFn:
    def plan_cut(state: PipelineState):
        project = project_of(state)

        # ① 内核先产出**证据**（plan_context.json）——它不做判断，只做普查
        evidence = client.plan_cut(project)
        if not evidence.ok:
            return _finish(S.PLAN_CUT, state, config, evidence)

        ctx = _read_json(project / "edit" / "plan_context.json", {}) or {}
        segs = ctx.get("segments") or []
        halluc = [s["idx"] for s in segs if s.get("suspect_hallucination")]

        header = [
            f"总段数：{len(segs)}",
            f"stats：{json.dumps(ctx.get('stats') or {}, ensure_ascii=False)}",
            "",
            "⚠️ 下面这些段是**幻觉嫌疑段**（时长 ≤0.5s 却 ≥6 字）——音频上其实是一整句连贯的话，",
            f"   **绝不许放进 drop_segment_ids**（放了会被内核直接拒绝）：{halluc or '（无）'}",
            "",
            "长静音（删除候选）："
            + json.dumps(ctx.get("long_gaps") or [], ensure_ascii=False)[:1200],
            "疑似重复块（试讲重复，通常整块删、保留后面正式版）："
            + json.dumps(ctx.get("repeat_clusters") or [], ensure_ascii=False)[:1200],
            "",
        ]

        # ⭐ 把**用户的原话**灌进提示词。
        #    为什么必须：做删留判断的是这个模型，而"用户到底要什么"只有主管知道。
        #    没有这一段，它只能按自己的观感猜 —— 实测后果是"用户只要加字幕，
        #    它却顺手删了 6 段"（越界）；反过来"用户点名要删的没删"同样会发生（漏做）。
        #    有原话，判断才有依据；而且要明确告诉它**用户没提的一律别删**。
        want = str((state.get("goal") or {}).get("user_request") or "").strip()
        if want:
            header += [
                "【用户的原话 —— 判断删留要照它来】",
                f"  {want}",
                "  ⇒ 用户点名要删的，删到位；**用户没提的，一律别删**。",
                "     多删是越界（少了内容是用户能再删的，多删的内容用户拿不回来）。",
                "  ⇒ 若用户要删的正好撞上上面的幻觉嫌疑段，不要硬删，在 abort 里写清理由。",
                "",
            ]
        rows = ["idx | start-end | dur | chars | flags | text"]
        for s in segs:
            flag = "HALLUC" if s.get("suspect_hallucination") else ""
            kw = ",".join(s.get("keywords") or [])
            text = str(s.get("text") or "").replace("\n", " ")[:40]
            rows.append(
                f"{s.get('idx')} | {s.get('start')}-{s.get('end')} | {s.get('dur')} | "
                f"{s.get('chars')} | {flag}{kw} | {text}"
            )
        user = "\n".join(header) + "\n" + evidence_block(
            "全段表（idx 是 0-based 原片下标，直接用于 drop_segment_ids）", rows)

        task = """\
【本次任务】判断哪几段该从**时间轴**上消失（删掉 = 观众看不到也听不到）。

只输出下面这个 JSON：

{
  "drop_segment_ids": [0, 12, ...],   // 0-based 原片下标；只为"对观众没有价值"的段
  "text_fixes": {"<idx>": "<改正后的文本>"},  // 只改**影响你判断删留**的文本
  "notes": "为什么这么删（一两句）",
  "abort": "<可选：若证据不足或与红线冲突，写清理由，其余字段留空>"
}

⛔ 四条硬约束（写成代码的、绕不过去）：
  1. 幻觉嫌疑段**绝不删时间轴**（上面已列出）——它们由字幕层合并处理；
  2. 保留块是**连续区间**概念：删段会把前后跨删除区的句子切断，所以要成块地删，
     不要逐句碎删；
  3. 段下标一律 **整数**，0-based，直连上面全段表的 idx；
  4. 上面若给了【用户的原话】，**照它来** —— 它优先于你自己的观感。
     用户没提删减、只说"加字幕/加动效"这类 ⇒ drop_segment_ids 就该是 **[]**。
"""
        try:
            llm = build_chat(config.model)
            raw = ask(llm, system=system_prompt_for_node(S.PLAN_CUT, task),
                      user=user + feedback_block(state, S.PLAN_CUT))
            verdict = parse_json(raw)
        except LLMError as e:
            return _llm_fail(S.PLAN_CUT, state, config, e, bad_output="JSON" in str(e))

        if verdict.get("abort"):
            return _finish(S.PLAN_CUT, state, config, synthetic(
                S.PLAN_CUT, ok=False, cause="reviewer_rejected",
                report={"ok": False, "cause": "reviewer_rejected",
                        "reviewer": verdict, "error": verdict.get("abort")}))

        # ── 契约预检（更早、更清楚的反馈；内核那道断言仍然在）──
        raw_drop = verdict.get("drop_segment_ids")
        if raw_drop is None:
            return _bad_output(S.PLAN_CUT, state, config,
                               "回复里缺 drop_segment_ids —— 即使一段都不删，也要给空数组 []")
        if not isinstance(raw_drop, list):
            return _bad_output(S.PLAN_CUT, state, config,
                              f"drop_segment_ids 必须是数组，实际 {type(raw_drop).__name__}")
        try:
            drop = sorted({int(x) for x in raw_drop})
        except (TypeError, ValueError):
            return _bad_output(S.PLAN_CUT, state, config,
                              f"drop_segment_ids 的元素必须是整数下标，实际 {raw_drop!r}")
        bad = [d for d in drop if d < 0 or d >= len(segs)]
        if bad:
            return _bad_output(
                S.PLAN_CUT, state, config,
                f"这些下标越界（合法范围 0..{len(segs) - 1}）：{bad[:20]}")

        raw_fixes = verdict.get("text_fixes")
        if raw_fixes is not None and not isinstance(raw_fixes, dict):
            return _bad_output(S.PLAN_CUT, state, config,
                              "text_fixes 必须是对象 {下标: 文本} 或省略")
        fixes = {str(k): str(v) for k, v in (raw_fixes or {}).items()}
        # 编排层代笔写决策文件（判断与执行分离）
        write_json(project / "edit" / "edit_decisions.json", {
            # ⚠️ `source` 是**源视频路径**，不是"谁写的这个文件"。
            #    内核 stage2 拿它当输入：`decisions.get("source") or "input/raw.mp4"`。
            #    我第一版写成 `"langgraph:plan_cut"`，于是 ffmpeg 的 `-i` 指向了一个
            #    不存在的文件 —— **真跑直接崩在 cut-apply**（exit 127）。
            #    ⚠️ 契约 §二 只列了字段名、没写含义，害我误读；内核的 hint（stage2 L115）
            #    才是准的：`{"source":"input/raw.mp4", "drop_segment_ids":[...]}`。
            #    所以：信息的归属另开一个键（`authored_by`），绝不复用内核消费的字段。
            "source": "input/raw.mp4",
            "authored_by": "langgraph:plan_cut",
            "drop_segment_ids": sorted(set(drop)),
            "text_fixes": fixes,
            "notes": str(verdict.get("notes") or ""),
        })
        return _finish(S.PLAN_CUT, state, config, synthetic(
            S.PLAN_CUT, ok=True,
            report={"ok": True, "drop_count": len(set(drop)), "text_fixes": len(fixes),
                    "notes": verdict.get("notes")}))

    return plan_cut


# ===========================================================================
#  字幕制作 · 纯文本纠错
# ===========================================================================
def make_plan_subtitles(client: KernelClient, config: AppConfig) -> NodeFn:
    def plan_subtitles(state: PipelineState):
        project = project_of(state)
        kept = _kept_index_texts(project)
        if not kept:
            return _finish(S.PLAN_SUBTITLES, state, config, synthetic(
                S.PLAN_SUBTITLES, ok=False, cause="missing_file",
                report={"ok": False, "cause": "missing_file",
                        "error": "没有保留段（cut_blocks.json 缺失或为空）—— 先跑剪裁"}))

        rows = [f"{idx} | {text[:60]}" for idx, text in kept]
        user = (evidence_block("保留段（idx | 文本）", rows)
                + feedback_block(state, S.PLAN_SUBTITLES))
        task = """\
【本次任务】只做**纯文本纠错**（专名 / 数字 / 同音字 / 明显错别字）。

⛔ 你不负责断句、不负责时间对齐、更不许重写字幕：
   字幕行 = Whisper 保留段本身，这由内核按红线生成，改动它等于破坏设计。

只输出下面这个 JSON：

{
  "text_fixes": {"<idx>": "<改正后的完整句子>"},   // 键是**原片下标**（下表左列）
  "cross_deleted_sentence": false,   // 是否发现"跨删除区的句子被切断、必须救回"
  "cross_detail": "<若上项为 true，指出是哪些 idx、为什么>",
  "abort": "<可选：证据不足或与红线冲突时写理由>"
}

⚠️ 若你判断某句被切断了（跨删除区），**不要自己去修**，把
`cross_deleted_sentence` 置 true —— 那属于**保留块**的问题，要回剪裁策划调整。
"""
        try:
            llm = build_chat(config.model)
            verdict = parse_json(ask(
                llm, system=system_prompt_for_node(S.PLAN_SUBTITLES, task),
                user=user))
        except LLMError as e:
            return _llm_fail(S.PLAN_SUBTITLES, state, config, e, bad_output="JSON" in str(e))

        if verdict.get("text_fixes") is not None and not isinstance(verdict["text_fixes"], dict):
            return _bad_output(S.PLAN_SUBTITLES, state, config,
                              "text_fixes 必须是对象 {下标: 改正后的文本} 或省略")

        if verdict.get("cross_deleted_sentence"):
            # 契约：这是**保留块**的问题 ⇒ 回剪裁策划；且计入跨 agent 回路（每片 ≤1）
            return _finish(S.PLAN_SUBTITLES, state, config, synthetic(
                S.PLAN_SUBTITLES, ok=False, cause="cross_deleted_sentence",
                report={"ok": False, "cause": "cross_deleted_sentence",
                        "error": str(verdict.get("cross_detail") or "")}))

        fixes = {str(k): str(v) for k, v in (verdict.get("text_fixes") or {}).items()}
        # ⚠️ 保留**用户点名改过**的条目（存在 `manual` 里）：那是人的意图，模型纠错不该盖掉它。
        #    没有这一步，"用户手动改一句 → 再让它整片纠一遍"会把用户的改动悄悄抹掉 ——
        #    静默丢失用户意图，比多一条冗余修正严重得多。
        #    冲突时以 manual 为准；只有模型上一轮自己写的（非 manual）才让位给这一轮的新结果。
        prev = _read_json(project / "edit" / "subtitle_fixes.json", {}) or {}
        manual = {str(k): str(v) for k, v in (prev.get("manual") or {}).items()}
        merged = {**fixes, **manual}
        write_json(project / "edit" / "subtitle_fixes.json",
                   {"source": "langgraph:plan_subtitles", "text_fixes": merged,
                    "manual": manual})
        return _finish(S.PLAN_SUBTITLES, state, config, synthetic(
            S.PLAN_SUBTITLES, ok=True,
            report={"ok": True, "text_fixes": len(merged), "manual_kept": len(manual)}))

    return plan_subtitles


# ===========================================================================
#  特效设计 · 动效工程
# ===========================================================================
def make_design_overlay(client: KernelClient, config: AppConfig) -> NodeFn:
    def design_overlay(state: PipelineState):
        project = project_of(state)
        goal = state.get("goal") or {}

        if not goal.get("overlay_required", True):
            # ⛔ 契约允许的唯一省略理由：用户明确要求无动效版（交付说明里要注明）
            return _finish(S.DESIGN_OVERLAY, state, config, synthetic(
                S.DESIGN_OVERLAY, ok=True,
                report={"ok": True, "skipped": "用户明确要求无动效版"}))

        timeline = _read_json(project / "edit" / "timeline_map.json", {}) or {}
        segs = timeline.get("segments") or []
        if not segs:
            return _finish(S.DESIGN_OVERLAY, state, config, synthetic(
                S.DESIGN_OVERLAY, ok=False, cause="missing_file",
                report={"ok": False, "cause": "missing_file",
                        "error": "edit/timeline_map.json 缺失或为空 —— 它是动效唯一可用的时间轴"}))

        rows = [f"{s.get('edit_start')}~{s.get('edit_end')} | {str(s.get('text'))[:60]}"
                for s in segs]
        timeline_block = evidence_block("剪后时间轴（edit_start~edit_end | 文本）", rows)
        words = []
        for s in segs[:200]:
            words += [f"{w.get('w')}@{w.get('s')}" for w in (s.get("words") or [])]
        task = f"""\
【本次任务】产出一份 HyperFrames 动效工程（`overlays/index.html`）。

片长（**剪后**时间轴）：{timeline.get('edit_duration')}s —— 工程时长必须等于它。

⛔ 时间**只能**取自下面这份剪后时间轴（`edit/timeline_map.json`）。
   绝不能用 `edit/transcript.json`（那是**剪前**的；每过一个剪点后面就累积偏移，
   用它做动画会越到后面错得越多）。

【HyperFrames 硬契约 —— 每条都被 `hyperframes lint` 检查】
⚠️ 违反这些**渲染过程本身不会报错**：轻则动画根本不播（产出静止画面），
   重则 `Composition has zero duration` 直接失败。所以一条都不能省。

1. 根元素（缺一不可）：
   <div id="root" data-composition-id="main" data-start="0"
        data-width="1920" data-height="1080" data-duration="{timeline.get('edit_duration')}">
2. 每个"计时元素"都要带 `data-start` + `data-duration`，并加 `class="clip"`
   （运行时靠这个类管理元素的可见性生命周期）。
   ⛔ **clip 元素的退场，必须在它的结束边界上再补一条 `tl.set(..., {{opacity:0}}, 边界秒数)` 硬杀。**
      为什么：退场通常正好淡到"下一个 clip 的开始秒"，而渲染是**逐帧 seek**（非线性），
      可能落在淡出**之后**，留下残余可见态 —— 成片里就是"上一张卡片没走干净"。
      `hyperframes lint` 会以 `gsap_exit_missing_hard_kill` 拒绝这种工程（渲染本身不报错）。
      写法：
          tl.to("#tip1", {{ x: -100, opacity: 0, duration: 0.4 }}, 26.98);   // 退场
          tl.set("#tip1", {{ opacity: 0 }}, 27.38);                          // 硬杀，27.38 = 下一个 clip 起点
      ⇒ **每个 clip 元素都要成对检查**：有退场 tween，就必须有落在它结束时刻上的 `tl.set`。

3. GSAP 时间线必须是**暂停**的，并注册到以 composition id 为键的注册表：
       const tl = gsap.timeline({{ paused: true }});
       ... tl.to(...) ...
       window.__timelines = window.__timelines || {{}};
       window.__timelines["main"] = tl;
   ⛔ 不注册 ⇒ 运行时发现不了它 ⇒ **动画根本不会播**。
4. ⛔ 只允许这四个方法：`set` / `to` / `from` / `fromTo`。
   （没有 `eventCallback` / `addLabel` / 嵌套 timeline —— 用了不会被驱动）
5. ⛔ 只允许这些属性：`opacity, x, y, scale, scaleX, scaleY, rotation, width, height, visibility`。
   想改颜色/描边/滤镜/clip-path/背景 —— 做不到，请用位移/缩放/透明度来表达同样的意思。
6. **初始隐藏态写在 CSS 里**（例：`.badge {{ opacity: 0 }}`）。
   ⛔ 不要写成 `tl.set(el, {{...}}, 0)`：零时长的 set 停在 0 位置时不渲染，
   第 0 帧会露出未隐藏状态（成片开头闪一下）。
7. GSAP 用**本地文件**：`<script src="./vendor/gsap.min.js"></script>`
   ⛔ 不要用 CDN —— 渲染时不该依赖外网可达性与速度。本机只预装了 GSAP，
   不要引第三方字体；需要字体就用 system-ui / "Microsoft YaHei"。
   ⛔ **但用了哪个字体族，就必须给它在 CSS 里声明一条 `@font-face`** —— 否则
      `hyperframes lint` 以 `font_family_without_font_face` 拒绝（渲染不报错，
      只是**静默回退到通用字体**，成片排版就不是你要的了）。
      系统自带的字体没有可下载文件，用 `src: local(...)` 即可，**声明本身就能过检查**：
          @font-face {{ font-family: "Microsoft YaHei"; src: local("Microsoft YaHei"); }}
      ⇒ 自查：`font-family` 里出现的**每一个**族名，都要能在上方找到同名的 `@font-face`。
         （写 `system-ui` / `sans-serif` 这类**通用族**不需要声明。）
8. 这是**透明叠加层**（ProRes4444 带 alpha）：背景必须真透明，不要铺满不透明底色。
9. 不遮挡区域：人脸中区 **x 700–1220** 不压；底部字幕区（y > 930）不压。

【设计意图】动效服务于"口播"：字幕已经在画面里了，你的层做的是**强调 / 点题 / 结构提示**。
   克制、与讲解节奏同步（用片尾 fade 收干净），不要喧宾夺主。

回复格式：先单独写一行 `{_HTML_MARKER}`，再**原样**给出完整 HTML（不要围栏、不要解释）。

{timeline_block}

词级时间抽样（词@秒）：
{' '.join(words[:400])}
"""
        # 提示词第 7 条承诺"用本地 GSAP"，但那必须由代码落实（模型装不了包）。
        vendor_note = _ensure_vendor_js(project)
        if not (project / "overlays" / "vendor" / VENDOR_JS).is_file():
            return _finish(S.DESIGN_OVERLAY, state, config, synthetic(
                S.DESIGN_OVERLAY, ok=False, cause="missing_vendor_js",
                report={"ok": False, "cause": "missing_vendor_js",
                        "error": f"本地找不到 {VENDOR_JS}（查过 overlays/vendor、"
                                 f"overlays/node_modules/gsap、{VENDOR_DIR}）—— "
                                 "工程引本地 GSAP，缺了会 `gsap is not defined` 渲染出静止画面"}))

        try:
            llm = build_chat(config.model)
            html = extract_html(ask(
                llm, system=system_prompt_for_node(S.DESIGN_OVERLAY, task),
                user="请按契约产出动效工程。" + feedback_block(state, S.DESIGN_OVERLAY)))
        except LLMError as e:
            return _llm_fail(S.DESIGN_OVERLAY, state, config, e,
                             bad_output="HTML" in str(e) or "标记" in str(e))

        # ⚠️ **先校验、再落盘**。次序反了会留下两份坏数据：
        #    ① 不合格的 index.html 已经写在盘上 —— 之后谁直接跑 overlay-render，
        #       渲染的就是这份坏工程；
        #    ② 它还会**覆盖掉上一份合格的**（回退重试时正是靠旧工程兜底）。
        # 先校验这两条最致命的契约（缺了动画根本不播，而且渲染不报错）：
        # lint 闸门在内核侧还有一道；这里给的是更早、更直白的反馈，省一次渲染往返。
        head = html[:4000]
        if "data-composition-id" not in head:
            return _bad_output(
                S.DESIGN_OVERLAY, state, config,
                "工程根元素缺 data-composition-id（运行时找不到 composition，动画不会播）。"
                "请按契约写成 <div id=\"root\" data-composition-id=\"main\" data-start=\"0\" "
                "data-width=\"1920\" data-height=\"1080\" data-duration=\"…\">")
        if "__timelines" not in html:
            return _bad_output(
                S.DESIGN_OVERLAY, state, config,
                "工程里没有 window.__timelines 注册 —— 运行时发现不了时间线，动画**根本不会播**。"
                "请补上 window.__timelines = window.__timelines || {}; "
                'window.__timelines["main"] = tl;')

        out = project / "overlays" / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")

        cmd = _finish(S.DESIGN_OVERLAY, state, config, synthetic(
            S.DESIGN_OVERLAY, ok=True,
            report={"ok": True, "index_html": str(out), "bytes": len(html.encode("utf-8")),
                    "vendor_note": vendor_note}))
        if vendor_note:
            merged = dict(cmd.update or {})
            merged["notes"] = list(merged.get("notes") or []) + [vendor_note]
            return Command(goto=cmd.goto, update=merged)
        return cmd

    return design_overlay


# ===========================================================================
#  剪裁策划 · 成片规格三件套
# ===========================================================================
def make_plan_final(client: KernelClient, config: AppConfig) -> NodeFn:
    def plan_final(state: PipelineState):
        project = project_of(state)
        timeline = _read_json(project / "edit" / "timeline_map.json", {}) or {}
        blocks = _read_json(project / "edit" / "cut_blocks.json", []) or []
        burn = (state.get("results") or {}).get(S.BURN_FULL, {}).get("report", {})

        task = f"""\
【本次任务】给出 **BGM 风格 + 混音参数**。文件路径由编排层决定，你不用管、也改不了。

片长：{timeline.get('edit_duration')}s；章节块数：{len(blocks)} 块

只输出下面这个 JSON：

{{
  "bgm": {{"style": "<≥20 字的纯器乐风格描述>", "max_price": 1.0}},
  "mix": {{"bgm_volume_sections": [{{"start": 0, "end": <片长>, "volume": 0.35}}],
           "bgm_fade_in": 0.5, "bgm_fade_out": 1.5, "bgm_loop": true}},
  "notes": "<一两句说明>"
}}

三条约束（都有内核断言兜着，但先写对能省一轮）：
  1. `bgm.style` **必须 ≥20 字**（付费护栏会拦），且必须是**纯器乐**描述；
  2. `bgm_volume_sections` 按**章节边界**分段（共 {len(blocks)} 块）。
     `volume` 是 BGM 的**线性增益**。判据不是"听感"，而是质检员用 ffmpeg 测出来的数字
     （**它听不到声音**，只能比数字）：

         Δ = 混音后的整体响度 − 纯人声响度（单位 LU）

     必须落在 **Δ ∈ [0.3, 4.0] LU** 之间，**两侧都会判失败**：
       · 偏低（Δ < 0.3）⇒ `bgm_silent`（"BGM 几乎没进去"）
       · 偏高（Δ > 4.0）⇒ `bgm_overpowers_voice`（抢戏、人声不清晰）

     ⛔ 所以**不要贴着任一侧给值**。真跑撞到过：给 0.2 实测只有 **+0.20 LU**，
        差 0.1 就过不去、整条片子被卡住。**实用区间约 0.3 ~ 0.5**：
        曲目本身偏轻就往上取，偏响就往下取；
  3. `bgm_fade_out` 必须 > 0 —— 片尾不淡出会被判 `bgm_abrupt_cut`（听感上像咔一声）。
"""
        try:
            llm = build_chat(config.model)
            verdict = parse_json(ask(
                llm, system=system_prompt_for_node(S.PLAN_FINAL, task),
                user="请给出 BGM 风格与混音参数。" + feedback_block(state, S.PLAN_FINAL)))
        except LLMError as e:
            return _llm_fail(S.PLAN_FINAL, state, config, e, bad_output="JSON" in str(e))

        bgm_in = verdict.get("bgm") or {}
        mix_in = verdict.get("mix") or {}
        style = str(bgm_in.get("style") or "").strip()
        if not style:
            return _bad_output(S.PLAN_FINAL, state, config,
                              "规格里缺 bgm.style（付费护栏要求 ≥20 字）")
        if len(style) < 20:
            return _bad_output(
                S.PLAN_FINAL, state, config,
                f"bgm.style 只有 {len(style)} 字，付费护栏要求 ≥20 字：{style!r}")

        # ── 以下全是**管道事实**，由代码写死（唯一实现在 nodes/specs.py）──
        facts = write_final_specs(
            project=project, style=style, mix_in=mix_in,
            total=float(timeline.get("edit_duration") or 0.0),
            max_price=float(bgm_in.get("max_price") or config.budgets.bgm_max_price),
        )
        return _finish(S.PLAN_FINAL, state, config, synthetic(
            S.PLAN_FINAL, ok=True, report={"ok": True, **facts}))

    return plan_final


# ===========================================================================
#  成片质检 · 两次审片
# ===========================================================================
def _collect_frames(
    project: Path, state: PipelineState, node: str, target_hint: str, *,
    prefixes: tuple[str, ...],
) -> tuple[list[Path], str]:
    """收集审片用的帧图，并**如实说明来源**。

    两级策略（顺序有讲究）：

      ① **本轮内核报告里给出的确切路径** —— 那是这次运行真正产出的帧，可信；
      ② 兜底扫描 `edit/tmpimg/` —— 只在①拿不到时才用。

    ⚠️ 为什么必须标注"用的是兜底"：`edit/tmpimg/` 是**跨阶段、跨轮次共用**的目录
    （`burn-test` 写 `frame_*.png`，`qc` 写 `qc_*.png` 与 `junction_*.png`，
    而且旧帧**不会被清理**）。兜底扫到的很可能是**上一轮**留下的帧 ——
    拿它去判"字幕有没有挡脸"，等于用一个不存在的证据下断言，而这类错误
    （断言看起来在跑、其实证据是错的）正是本项目最忌讳的一种。

    所以兜底分支做了两件事：**只认本该由本类阶段产出的前缀**，
    并且把来源写进报告 —— 偏离要留痕，不能装成"证据齐全"。
    """
    frames: list[Path] = []
    for src_node in (node, target_hint):
        report = (state.get("results") or {}).get(src_node, {}).get("report") or {}
        for key in ("frames", "junction_frames"):
            for p in report.get(key) or []:
                path = Path(p)
                if path.is_file():
                    frames.append(path)
    if frames:
        return frames[:8], "本轮内核报告给出的确切路径"

    tmpimg = project / "edit" / "tmpimg"
    if not tmpimg.is_dir():
        return [], "无（edit/tmpimg 不存在）"
    cands = [p for p in tmpimg.glob("*.png") if p.name.startswith(prefixes)]
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    taken = cands[:8]
    return taken, (
        f"⚠️ 兜底扫描（edit/tmpimg 跨阶段共用且旧帧不清理，"
        f"取最新 {len(taken)} 张 —— **不保证**是本轮产生的帧）"
    )


def _route_review(node: str, state: PipelineState, config: AppConfig, verdict: dict):
    """把审片结论翻成裁决，返回 `Command`（审片这一段的**唯一**决策点）。

    优先级：**矩阵内的 cause 优先**（这样重试走的是与别处完全一致的预算与回退路径），
    其次是评审直接指定的合法目标（走 `decide_manual`，但仍过同一份护栏），
    都没有 ⇒ 停下问人（不许"看不懂就放行"）。
    """
    if verdict.get("pass"):
        return _finish(node, state, config, synthetic(
            node, ok=True, report={"ok": True, "reviewer": verdict}))

    cause = str(verdict.get("cause") or "").strip()
    target = str(verdict.get("target") or "").strip()
    reasons = "；".join(str(r) for r in (verdict.get("reasons") or [])) or "（未给理由）"
    result = synthetic(node, ok=False, cause=cause or "reviewer_rejected",
                       report={"ok": False, "cause": cause, "reviewer": verdict})

    if cause in R.MATRIX:
        goto, update = R.advance(node=node, result=result, state=state, config=config)
    elif target in S.WORKER_NODES:
        d = R.decide_manual(node=node, target=target,
                            cause=cause or "reviewer_rejected",
                            reason=f"成片质检判不通过：{reasons}",
                            state=state, config=config)
        goto, update = R.advance(node=node, result=result, state=state, config=config,
                                 decision=d)
    else:
        d = R._halt(  # noqa: SLF001 —— 同包内的裁决构造器，故意只留一份
            node,
            f"成片质检判不通过，但既没给出矩阵内的 cause（得到 {cause!r}），"
            f"也没给合法回退目标（得到 {target!r}）⇒ 无法裁决，停下让人看。理由：{reasons}",
            cause=cause or "reviewer_rejected")
        goto, update = R.advance(node=node, result=result, state=state, config=config,
                                 decision=d)

    # 字幕挡脸时，评审给的 MarginV 要**带到重烧那一步**，否则白绕一圈
    if verdict.get("margin_v") is not None:
        update["revision"] = {"margin_v": float(verdict["margin_v"])}
    return command(goto, update)


def _review_node(
    node: str, config: AppConfig, *, target_hint: str, extra_task: str,
    frame_prefixes: tuple[str, ...],
) -> NodeFn:
    def review(state: PipelineState):
        project = project_of(state)
        frames, source = _collect_frames(project, state, node, target_hint,
                                        prefixes=frame_prefixes)
        if not frames:
            return _finish(node, state, config, synthetic(
                node, ok=False, cause="qc_probe_failed",
                report={"ok": False, "cause": "qc_probe_failed",
                        "error": "没有可看的帧图 —— 无法审片（不得当作通过）"}))

        qc = _read_json(project / "edit" / "qc_report.json", {}) or {}
        digital = {
            "ok": qc.get("ok"), "causes": qc.get("causes"),
            "failed_checks": [c for c in (qc.get("checks") or []) if not c.get("ok")],
        }
        task = f"""\
【本次任务】审片：判断**能否进入下一步**。

你不必（也无法）听声音 —— BGM 是否进去、是否盖住人声、片尾是否硬切，
这些已经由内核做成**数字断言**，下面一并给你。你负责的是**看图**：
字幕是否真烧进画面、是否挡脸、接缝是否跳帧、动效层是否遮脸/压字幕/掉帧。

数字断言（来自 kb_qc）：{json.dumps(digital, ensure_ascii=False)[:2000]}

{_VERDICT_CONTRACT}
可用的 cause 菜单（从回退矩阵生成，选它就能走与别处一致的回退路径）：
{_review_cause_menu()}

{extra_task}
"""
        try:
            llm = build_chat(config.model, vision=True)
            verdict = parse_json(ask(
                llm, system=system_prompt_for_node(node, task),
                user=(f"请审这 {len(frames)} 帧（按时间顺序）。"
                      + feedback_block(state, node)),
                images=frames[:8]))
        except LLMError as e:
            return _llm_fail(node, state, config, e, bad_output="JSON" in str(e))

        # `_route_review` 直接返回 `Command`（它是这一段的唯一决策点），
        # 这里只额外挂上"这轮看了哪几帧"作为证据 —— 必须**重建** Command，
        # 不能去改它的字段（Command 是给运行时消费的，就地改容易漏）。
        cmd = _route_review(node, state, config, verdict)
        merged = dict(cmd.update or {})
        merged["review_frames"] = [str(p) for p in frames]
        merged["review_frames_source"] = source
        if source.startswith("⚠️ 兜底"):
            # 证据来源有疑点 ⇒ 留痕给用户（别让"看起来有证据"盖过"证据可能不对"）
            merged["notes"] = list(merged.get("notes") or []) + [f"{node} 的帧图来源：{source}"]
        return Command(goto=cmd.goto, update=merged)

    return review


def make_llm_nodes(client: KernelClient, config: AppConfig) -> dict[str, NodeFn]:
    return {
        S.PLAN_CUT: make_plan_cut(client, config),
        S.PLAN_SUBTITLES: make_plan_subtitles(client, config),
        S.DESIGN_OVERLAY: make_design_overlay(client, config),
        S.PLAN_FINAL: make_plan_final(client, config),
        # 兜底扫描时只认本类阶段的前缀（详见 `_collect_frames`）：
        #   burn-test 产 `frame_*.png`；qc 产 `qc_*.png` 与 `junction_*.png`
        S.REVIEW_TEST: _review_node(
            S.REVIEW_TEST, config, target_hint=S.BURN_TEST, frame_prefixes=("frame_",),
            extra_task="这是 **10s 测试片**（CP2）。字幕是否真烧进画面是这里唯一要判的硬事实 ——"
                       "没通过就**绝不能**烧全片。"),
        S.REVIEW_FINAL: _review_node(
            S.REVIEW_FINAL, config, target_hint=S.RUN_QC,
            frame_prefixes=("qc_", "junction_"),
            extra_task="这是**全片**（CP3）。除了字幕，还要看**动效层**是否遮脸/压字幕/掉帧 ——"
                       "动效层不得默认跳过，它是成片质量的必需一环。"),
    }
