"""阶段登记表 —— 图节点名的**唯一事实源**。

为什么要单独一个文件：回退矩阵、图装配、状态、测试、给 LLM 的提示词都要引用
这些阶段名。名字散落在各文件里字符串手写，迟早出现 `"burn_test"` / `"burnTest"`
这种对不上、而且**静默**（路由查不到就退化成"不知道该回退到哪"）。
所以统一在这里定义常量，并由 `matrix_targets()` 反查，测试会断言矩阵里引用的
每个目标都是真实存在的节点。

角色归属也在这里 —— 它是 `docs/contracts.md` §五「子代理契约」的编码版，
回退矩阵的"回退目标"就是靠它翻译成"找哪个 agent"。
"""

from __future__ import annotations

# ============================================================================
#  图节点名 = 契约里的阶段
# ============================================================================
PROBE = "probe"                        # 主管：kb_status（只读，断点续跑的依据）
TRANSCRIBE = "transcribe"              # 剪裁策划：kb_transcribe
PLAN_CUT = "plan_cut"                  # 剪裁策划：kb_plan_cut + LLM 写 edit_decisions.json
APPLY_CUT = "apply_cut"                # 剪裁策划：kb_cut(run=True)
PLAN_SUBTITLES = "plan_subtitles"      # 字幕制作：LLM 写 subtitle_fixes.json
BUILD_SRT = "build_srt"                # 字幕制作：kb_srt
BURN_TEST = "burn_test"                # 字幕制作：kb_burn(mode=test)
REVIEW_TEST = "review_test"            # 成片质检：LLM 看图（CP2）
BURN_FULL = "burn_full"                # 字幕制作：kb_burn(mode=full)
DESIGN_OVERLAY = "design_overlay"      # 特效设计：LLM 写 overlays/index.html
RENDER_OVERLAY = "render_overlay"      # 特效设计：kb_overlay
PLAN_FINAL = "plan_final"              # 剪裁策划：LLM 写 edit/bgm.json + mix.json + assemble.json
GENERATE_BGM = "generate_bgm"          # 剪裁策划：kb_bgm（唯一花钱 + 人工闸门）
ASSEMBLE_BASE = "assemble_base"        # 剪裁策划：kb_assemble(mode=assemble) 缝接骨架
MIX_BGM = "mix_bgm"                    # 剪裁策划：kb_assemble(mode=mix) → 无特效版成片
APPLY_OVERLAY = "apply_overlay"        # 特效设计：kb_assemble(mode=assemble, +overlays)
RUN_QC = "run_qc"                      # 主管：kb_qc(target=final)
REVIEW_FINAL = "review_final"          # 成片质检：LLM 看图 + 数字断言（CP3）
DELIVER = "deliver"                    # 主管：交付
HALT = "halt"                          # 主管：停下并如实报告

# ⭐ 人工拍板的**独立节点**（不属于 WORKER：它不产生内核结果，只负责"停下来问人"）。
#
# 为什么不把 interrupt() 塞进普通节点里：LangGraph 在恢复时**会把节点函数从头重跑**
# （`interrupt()` 的返回值是那次重跑才拿到的）。如果批准逻辑写在"跑内核 → 发现更贵 →
# 询问"的节点里，用户一点头，那个内核调用就会被**再跑一遍** —— 对 30 分钟的编码来说
# 这是不可接受的代价。独立节点里只有读操作，重跑无副作用。
APPROVAL = "request_approval"          # 主管：请用户拍板（付费 / 更贵的动作）

# ⛔ 例外放行（`kb_override`）—— 主管独有，必须写明理由。
#    放在 PROBE 之后、续跑目标之前执行：因为它会**改变续跑的意义**
#    （重转写会让剪辑决策/时间轴/字幕全部作废，续跑点必须跟着前移）。
OVERRIDE = "override"

# 所有"会产生内核调用结果、需要被路由"的节点（= 回退矩阵可能指向的目标）
WORKER_NODES: tuple[str, ...] = (
    PROBE, TRANSCRIBE, PLAN_CUT, APPLY_CUT,
    PLAN_SUBTITLES, BUILD_SRT, BURN_TEST, REVIEW_TEST, BURN_FULL,
    DESIGN_OVERLAY, RENDER_OVERLAY,
    PLAN_FINAL, GENERATE_BGM, ASSEMBLE_BASE, MIX_BGM, APPLY_OVERLAY,
    RUN_QC, REVIEW_FINAL,
)

# ============================================================================
#  角色归属（contracts.md §五）
# ============================================================================
CUT_PLANNER = "剪裁策划"
SUBTITLE_MAKER = "字幕制作"
OVERLAY_DESIGNER = "特效设计"
QC_INSPECTOR = "成片质检"
DIRECTOR = "主管"

OWNER: dict[str, str] = {
    PROBE: DIRECTOR,
    TRANSCRIBE: CUT_PLANNER,
    PLAN_CUT: CUT_PLANNER,
    APPLY_CUT: CUT_PLANNER,
    PLAN_SUBTITLES: SUBTITLE_MAKER,
    BUILD_SRT: SUBTITLE_MAKER,
    BURN_TEST: SUBTITLE_MAKER,
    BURN_FULL: SUBTITLE_MAKER,
    REVIEW_TEST: QC_INSPECTOR,
    DESIGN_OVERLAY: OVERLAY_DESIGNER,
    RENDER_OVERLAY: OVERLAY_DESIGNER,
    PLAN_FINAL: CUT_PLANNER,
    GENERATE_BGM: CUT_PLANNER,
    ASSEMBLE_BASE: CUT_PLANNER,
    MIX_BGM: CUT_PLANNER,
    APPLY_OVERLAY: OVERLAY_DESIGNER,
    RUN_QC: DIRECTOR,
    REVIEW_FINAL: QC_INSPECTOR,
    DELIVER: DIRECTOR,
    HALT: DIRECTOR,
    APPROVAL: DIRECTOR,
    OVERRIDE: DIRECTOR,
}


def owner_of(node: str) -> str:
    """节点的负责角色；未登记则明确报出来（而不是返回 None 让上层静默走偏）。"""
    try:
        return OWNER[node]
    except KeyError:
        raise KeyError(f"未登记的阶段：{node!r} —— 请先在 stages.OWNER 里登记它") from None


# ============================================================================
#  正常推进顺序（happy path）
# ============================================================================
# ⚠️ 这张表描述的是**单链**顺序（当前实现就是单链：字幕线跑完再跑特效线）。
#    契约允许"特效线与字幕线并行"，但那是**可选优化**、不是必需项（原 DSH 版也是串行）。
#    真要做并行时，改这张表 + 在 `graph.py` 里给 APPLY_CUT 加分叉边即可；
#    状态已按并发安全写（所有共享键都带 reducer），不需要重构。
FOLLOWING: dict[str, str] = {
    TRANSCRIBE: PLAN_CUT,
    PLAN_CUT: APPLY_CUT,
    APPLY_CUT: PLAN_SUBTITLES,
    PLAN_SUBTITLES: BUILD_SRT,
    BUILD_SRT: BURN_TEST,
    BURN_TEST: REVIEW_TEST,
    REVIEW_TEST: BURN_FULL,
    BURN_FULL: DESIGN_OVERLAY,
    DESIGN_OVERLAY: RENDER_OVERLAY,
    RENDER_OVERLAY: PLAN_FINAL,
    PLAN_FINAL: GENERATE_BGM,
    GENERATE_BGM: ASSEMBLE_BASE,
    ASSEMBLE_BASE: MIX_BGM,
    MIX_BGM: APPLY_OVERLAY,
    APPLY_OVERLAY: RUN_QC,
    RUN_QC: REVIEW_FINAL,
    REVIEW_FINAL: DELIVER,
}

# 完整单链（供测试断言：链上每一个节点都有后继，且无环）
CHAIN: tuple[str, ...] = (
    PROBE, TRANSCRIBE, PLAN_CUT, APPLY_CUT,
    PLAN_SUBTITLES, BUILD_SRT, BURN_TEST, REVIEW_TEST, BURN_FULL,
    DESIGN_OVERLAY, RENDER_OVERLAY,
    PLAN_FINAL, GENERATE_BGM, ASSEMBLE_BASE, MIX_BGM, APPLY_OVERLAY,
    RUN_QC, REVIEW_FINAL, DELIVER,
)


# ============================================================================
#  成片链路上的固定产物路径 —— **管道事实，不由 LLM 决定**
# ============================================================================
# 契约只说"剪裁策划给 segments、特效设计加 overlays"，没说清"最终输出叫什么".
# 真跑暴露过两个后果：没人把 output 改成 final.mp4 ⇒ **成片永不产出**；
# 或 segments 指向混音前的视频 ⇒ **静默没有 BGM**。两件都是管道一致性，归代码。
SUBTITLED_FULL = "renders/subtitled_full.mp4"   # 烧完字幕的全片（带人声）
SKELETON_VIDEO = "renders/skeleton.mp4"         # 缝接骨架（单段 = -c copy，保留音频）
MIXED_MASTER = "renders/assembled.mp4"          # 铺完 BGM 的主轨（无特效版成片）
FINAL_VIDEO = "renders/final.mp4"               # 叠加动效后的成片（唯一交付物）


# ============================================================================
#  断点续跑：`kb_status.next_stage` → 从哪个节点接续
# ============================================================================
# 内核给的阶段名（`kb.py cmd_status` 的 stages 键）与图节点不是一一对应，
# 所以显式翻译一次。取舍见注释。
RESUME_MAP: dict[str, str] = {
    "1_transcribe": TRANSCRIBE,
    "2_plan": PLAN_CUT,      # plan_cut 会重跑 kb_plan_cut（幂等），再让 LLM 重新判断
    "2_decide": PLAN_CUT,
    "2_apply": APPLY_CUT,
    # ⚠️ 3_srt 之后返回 PLAN_SUBTITLES 而不是 BUILD_SRT：
    #    字幕修复文件是字幕制作的产物，跳过它等于让 LLM 的纠错环节凭空消失。
    "3_srt": PLAN_SUBTITLES,
    # ⚠️ 4_burn / 6_qc 一律回到 BURN_TEST，**不直接跳 burn_full**：
    #    烧全片有硬护栏（必须有测试片 + 抓帧证据），直接跳过去会被拒。
    #    重出一次 10s 测试片只要 1 分钟，是这里最便宜的保险。
    "4_burn": BURN_TEST,
    # ⚠️ 5_overlay ⇒ DESIGN_OVERLAY（不是 RENDER_OVERLAY）：
    #    "没有 MOV"既可能是"工程还没设计"，也可能是"设计了没渲染成功"。
    #    从设计那一步接续两种情况都能覆盖；直接跳到渲染，若 index.html 也不在就会被拒。
    "5_overlay": DESIGN_OVERLAY,
    "6_qc": BURN_TEST,
    "done": DELIVER,
}


def resume_node(next_stage: str | None) -> str:
    """把内核的 `next_stage` 翻译成接续节点；未知值一律从头（转写）开始。"""
    if not next_stage:
        return TRANSCRIBE
    return RESUME_MAP.get(next_stage, TRANSCRIBE)


def matrix_targets() -> set[str]:
    """所有可作为回退目标的节点名（供测试断言矩阵不引用不存在的节点）。"""
    return set(WORKER_NODES) | {DELIVER, HALT, APPROVAL, OVERRIDE}


def known_nodes() -> set[str]:
    """图里存在的全部节点。"""
    return set(WORKER_NODES) | {DELIVER, HALT, APPROVAL, OVERRIDE}


# 例外放行的两个动作（与 `kb_override` 的内核子命令一一对应）
OVERRIDE_RETRANSCRIBE = "retranscribe"
OVERRIDE_ALLOW_HALLUCINATION_CUT = "allow_hallucination_cut"

# 各动作执行完之后，**续跑点要前移到哪**
#   · 重转写 ⇒ 剪辑决策 / 剪后时间轴 / 字幕全部作废（契约原话）⇒ 回到重新判断删留
#   · 放行删幻觉段 ⇒ 剪切产物变了 ⇒ 从重做字幕起（build_srt 会按新 cut_blocks 重映射）
OVERRIDE_RESUME: dict[str, str] = {
    OVERRIDE_RETRANSCRIBE: PLAN_CUT,
    OVERRIDE_ALLOW_HALLUCINATION_CUT: PLAN_SUBTITLES,
}
