"""回退裁决 —— 把 `docs/contracts.md` §三 / §三补 的回退矩阵**编码成数据**，外加预算护栏。

为什么做成数据而不是 if/elif 链：

    1. 这张矩阵是**三方对齐的单一事实源**（文档 / 实现 / 测试）。数据化之后，
       测试可以**遍历**它断言"每个 cause 都有目标、每个目标都真实存在、每条 human
       都写清了理由" —— 写成 if/elif 就只能靠人肉读代码，而人肉读不出漏项。
    2. 交付时要把"每个 cause 的处理记录"给用户看（contracts §三 结尾的要求）。
       矩阵一旦是数据，这份记录就是**自然产出**，不必再写一遍、也不会与实现漂移。

三条裁决纪律（来自 `agents/director.md`）：

    · **工具能确定性修好的，绝不叫 LLM 重做** —— `fix` / `fix_param` 直接重跑那个确定性阶段；
    · **只有"判断错了"才回退到子代理** —— `rethink` 才带上 LLM 重新判断；
    · **更贵的动作必须先问用户** —— `expensive=True` 的路由一律先中断确认（红线第 8 条）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import stages as S
from .config import AppConfig
from .kernel import KernelResult
from .state import (
    PipelineState, consecutive_same_cause, cross_agent_count, retakes_of,
)

# ---------------------------------------------------------------------------
#  路由类别
# ---------------------------------------------------------------------------
HUMAN = "human"        # 不是图节点：表示"停下、升级人工"，禁止自动重试

KIND_OK = "ok"                 # 通过
KIND_FIX = "fix"               # 工具级确定性修复（重跑同一个确定性阶段）
KIND_FIX_PARAM = "fix_param"   # 工具级修复，但需要一个由评审给出的参数（如 MarginV）
KIND_RETHINK = "rethink"       # 回子代理重新判断（要带上失败证据）
KIND_RESUME = "resume"         # 同一节点继续（续轮询 / 超时重试）—— 不消耗回退预算
KIND_AGENT_LOOP = "agent_loop"  # 跨 agent 回路（改保留块 ⇒ 字幕全废、成片重编）
KIND_BENIGN = "benign"         # 不是错误（已有产物 / 未确认付费等语义分支）
KIND_HUMAN = "human"           # 停下升级人工

COST_FREE = "free"
COST_LOW = "low"
COST_MID = "mid"
COST_HIGH = "high"


@dataclass(frozen=True)
class Route:
    """一条回退规则。`target` = 回退到哪个节点；`by_node` = "按谁挂的"分别决定。"""

    cause: str
    kind: str
    cost: str
    note: str
    target: str | None = None
    # 同一个 cause 在不同阶段挂着时，该回退到不同地方（典型：`bad_spec` 要看是哪个文件写错了）
    by_node: dict[str, str] = field(default_factory=dict)
    fallback: str = HUMAN
    expensive: bool = False     # 更贵的动作 ⇒ 必须先问用户
    cross_agent: bool = False   # 计入"跨 agent 回路每片 ≤1"

    def target_for(self, node: str) -> str | None:
        if self.by_node:
            return self.by_node.get(node, self.fallback)
        return self.target


def _r(cause: str, kind: str, cost: str, note: str, **kw) -> tuple[str, Route]:
    return cause, Route(cause=cause, kind=kind, cost=cost, note=note, **kw)


# ============================================================================
#  回退矩阵（逐条对应 contracts.md §三）
# ============================================================================
MATRIX: dict[str, Route] = dict([
    # ── 字幕层：全是工具级确定性修复，**不要让 LLM 重写字幕** ──────────────
    _r("srt_bom", KIND_FIX, COST_LOW,
       "SRT 带 BOM ⇒ libass 全片无字幕；重跑 kb_srt（确定性修复）", target=S.BUILD_SRT),
    _r("srt_trailing_symbol", KIND_FIX, COST_LOW,
       "句末带标点；重跑 kb_srt", target=S.BUILD_SRT),
    _r("srt_timeline_overlap", KIND_FIX, COST_LOW,
       "字幕时间重叠；重跑 kb_srt", target=S.BUILD_SRT),
    _r("srt_empty", KIND_RETHINK, COST_MID,
       "重映射后一条字幕都不剩 ⇒ 几乎一定是**保留块**判断出了问题；"
       "回剪裁策划（⛔ 不许手写 SRT）", target=S.PLAN_CUT),

    # ── 剪辑层：保留块 / 切点的问题 ────────────────────────────────────────
    _r("cut_duration_mismatch", KIND_RETHINK, COST_MID,
       "实测时长 ≠ 保留块计划 ⇒ 回剪裁策划复核保留块", target=S.PLAN_CUT),
    _r("srt_timeline_mismatch", KIND_RETHINK, COST_MID,
       "字幕整体与画面脱节 ⇒ 回剪裁策划", target=S.PLAN_CUT),
    _r("junction_glitch", KIND_RETHINK, COST_MID,
       "切点吞字/跳帧 ⇒ 回剪裁策划把切点回退 0.1~0.3s 重跑（接缝突兀几乎总是"
       "\"把自然气口剪得太净、或切在发音中间\"）", target=S.PLAN_CUT),
    _r("audio_clipping", KIND_RETHINK, COST_MID,
       "削顶爆音 ⇒ 同 junction_glitch，回剪裁策划调整切点", target=S.PLAN_CUT),
    _r("av_desync", KIND_RETHINK, COST_MID,
       "音视频轨时长不一致 ⇒ 同 junction_glitch", target=S.PLAN_CUT),
    _r("index_out_of_range", KIND_RETHINK, COST_LOW,
       "删除下标越界（多半把 0-based 写成了 1-based）⇒ 回剪裁策划改下标重跑",
       target=S.PLAN_CUT),
    _r("empty_keep_set", KIND_RETHINK, COST_LOW,
       "删除集覆盖了全部片段 ⇒ 回剪裁策划复核保留块（不可能没人说话）",
       target=S.PLAN_CUT),
    _r("hallucination_cut_forbidden", KIND_RETHINK, COST_LOW,
       "删除集里含幻觉嫌疑段（时长 ≤0.5s 却 ≥6 字）⇒ 把那些下标**移除**，"
       "它们会在字幕层自动合并。⛔ 不是\"再删一次\"；确需删除只能由主管走 kb_override",
       target=S.PLAN_CUT),
    _r("transcript_bad_shape", KIND_FIX, COST_LOW,
       "逐字稿不是非空段列表 ⇒ 先跑 kb_transcribe", target=S.TRANSCRIBE),
    _r("transcript_missing_timestamps", KIND_FIX, COST_MID,
       "逐字稿缺词级时间戳（动效/字幕都要用）⇒ 重跑 kb_transcribe",
       target=S.TRANSCRIBE),
    _r("transcript_exists", KIND_BENIGN, COST_FREE,
       "**不是错误**：已有逐字稿 ⇒ 直接复用（重转写会作废整条下游）",
       target=S.PLAN_CUT),
    _r("cross_deleted_sentence", KIND_AGENT_LOOP, COST_HIGH,
       "字幕制作报「跨删除区句子必须救回」⇒ 这是**保留块判断**的问题，回剪裁策划调整保留块。"
       "⛔ 代价：保留块一改，字幕全废、edited_final.mp4 要重新编码 —— 整片只允许走一次",
       target=S.PLAN_CUT, cross_agent=True),

    # ── 烧录层 ────────────────────────────────────────────────────────────
    _r("subtitle_occlusion", KIND_FIX_PARAM, COST_LOW,
       "字幕挡脸 ⇒ **只调解离底距离 MarginV** 后重烧（它是离底边距离，"
       "**越大越往上**；字幕太高要减小它，不是增大）。除 MarginV 外禁止自调样式",
       target=S.BURN_TEST),
    _r("burn_duration_mismatch", KIND_FIX, COST_LOW,
       "烧录本不该改变时长，偏差 >0.5s 属异常 ⇒ 重烧一次（同因复发即停下报人）",
       target=S.BURN_FULL),

    # ── 装配层 ────────────────────────────────────────────────────────────
    _r("assemble_duration_mismatch", KIND_RETHINK, COST_MID,
       "预测与实测偏差 >0.5s ⇒ 说明 xfade 递推或切点算错了（这是 D-1 的防错网）⇒"
       "回剪裁策划复核 assemble.json 与保留块", target=S.PLAN_FINAL),
    _r("mix_duration_mismatch", KIND_RETHINK, COST_LOW,
       "混音后时长被带跑 ⇒ 回剪裁策划复核 mix.json 参数后重跑 mix",
       target=S.PLAN_FINAL),
    _r("black_frame", KIND_FIX, COST_MID,
       "首尾黑帧 ⇒ 回剪裁策划重跑缝接（装配层）", target=S.ASSEMBLE_BASE),

    # ── 动效层 ────────────────────────────────────────────────────────────
    _r("missing_overlay", KIND_FIX, COST_LOW,
       "动效层文件缺失 ⇒ 重跑 kb_overlay 渲染", target=S.RENDER_OVERLAY),
    _r("missing_overlay_project", KIND_RETHINK, COST_LOW,
       "overlays/index.html 不存在 ⇒ 回特效设计产出工程", target=S.DESIGN_OVERLAY),
    _r("missing_vendor_js", KIND_HUMAN, COST_FREE,
       "本地找不到 GSAP（`overlays/vendor/gsap.min.js`）⇒ **停下**：动效工程引的是本地文件，"
       "缺了会 `gsap is not defined`（渲染出静止画面，而且不报错）。"
       "⛔ 不要改回 CDN —— 那会让渲染依赖外网可达性"),
    _r("hyperframes_lint_failed", KIND_RETHINK, COST_LOW,
       "动效工程没过 hyperframes 自带的 lint ⇒ 回特效设计按报出的每条 Fix 改 "
       "`overlays/index.html`。⚠️ 这类问题**渲染不会报错**，只会安静地产出空/静止的 MOV "
       "（缺 `window.__timelines` 注册、缺 `data-start`、缺时长来源、初始态写在时间线里…），"
       "所以必须在渲染前挡住 —— 内核已经在 overlay-render 里加了这道闸门",
       target=S.DESIGN_OVERLAY),
    _r("hyperframes_render_failed", KIND_RETHINK, COST_MID,
       "动效渲染失败 ⇒ 回特效设计（多半是根元素缺 data-composition-id、"
       "或用了 Google Fonts <link>）", target=S.DESIGN_OVERLAY),
    _r("missing_hyperframes", KIND_HUMAN, COST_FREE,
       "本地没装 hyperframes（要在 overlays/ 下 npm install）⇒ **停下升级人工**，"
       "不是 LLM 能修的"),

    # ── BGM（唯一花钱）────────────────────────────────────────────────────
    _r("bgm_exists", KIND_BENIGN, COST_FREE,
       "**不是错误**：已有可用 BGM ⇒ 免费复用，直接去装配（不会重复扣费）",
       target=S.ASSEMBLE_BASE),
    _r("bgm_needs_confirm", KIND_BENIGN, COST_FREE,
       "**不是错误**：付费操作未确认。正确做法是先看价、由用户拍板后再提交"
       "（本应用的作法是**中断询问**，而不是自动补上确认）"),
    _r("bgm_missing", KIND_FIX, COST_LOW,
       "BGM 素材不存在 ⇒ 回剪裁策划先跑 kb_bgm 生成（或取回用户指定的曲目）",
       target=S.GENERATE_BGM),
    _r("bgm_timeout", KIND_RESUME, COST_LOW,
       "生成任务超时（未 final）⇒ **再调一次就是续轮询**（不会重复扣费），"
       "⛔ 绝不能重新提交", target=S.GENERATE_BGM),
    _r("bgm_generate_failed", KIND_FIX, COST_LOW,
       "生成失败 ⇒ 先看 bgm/task.json 与响应体；确认未创建任务才可重试",
       target=S.GENERATE_BGM),
    _r("bgm_submit_failed", KIND_FIX, COST_LOW,
       "提交失败 ⇒ 同上（若已解析出 task_id，再调即为续轮询）", target=S.GENERATE_BGM),
    _r("bgm_download_failed", KIND_FIX, COST_LOW,
       "下载失败 ⇒ 重跑 kb_bgm（任务已完成，不会重复扣费）", target=S.GENERATE_BGM),
    _r("bgm_silent", KIND_RETHINK, COST_LOW,
       "混音后与纯人声几乎无差 ⇒ BGM 没进去；回剪裁策划复核 mix.json 重跑 mix",
       target=S.PLAN_FINAL),
    _r("bgm_overpowers_voice", KIND_RETHINK, COST_LOW,
       "BGM 抢戏 ⇒ 回剪裁策划下调对应 section 的 volume 重跑 mix", target=S.PLAN_FINAL),
    _r("bgm_true_peak", KIND_RETHINK, COST_LOW,
       "真峰值超 −1 dBFS ⇒ 回剪裁策划整体降音量重跑 mix", target=S.PLAN_FINAL),
    _r("bgm_abrupt_cut", KIND_RETHINK, COST_LOW,
       "音乐中途断掉/片尾被硬切 ⇒ 回剪裁策划换够长的音乐或补 bgm_fade_out 重跑 mix",
       target=S.PLAN_FINAL),
    _r("bgm_price_above_budget", KIND_RETHINK, COST_LOW,
       "单价超 max_price ⇒ 回剪裁策划：改用用户提供的曲目，或**向用户申请**调预算。"
       "⛔ 不许硬顶着花钱、也不许靠改配置绕过", target=S.PLAN_FINAL, expensive=True),
    _r("bgm_insufficient_balance", KIND_HUMAN, COST_FREE,
       "网关余额不足（402）⇒ **停下升级人工**：充值或改用现成曲目。⛔ 禁止反复提交"),
    _r("bgm_no_api_key", KIND_HUMAN, COST_FREE,
       "找不到网关密钥 ⇒ **停下升级人工**（设 LK_API_KEY / LK_API_KEY_FILE）"),
    _r("missing_user_bgm", KIND_HUMAN, COST_FREE,
       "指定了自备 BGM（`--bgm-source user`）但 `bgm/` 下没有音频文件 ⇒ **停下**："
       "把曲目放进去，或改用生成。⛔ **不要自动改成生成** —— 那会花钱，"
       "而且违背用户「用我自己那首」的明确选择。"
       "（这一条原来是死循环：跳过生成 → mix 报 bgm_missing → 回退到生成节点 → 又跳过）"),
    _r("bgm_pricing_unavailable", KIND_HUMAN, COST_FREE,
       "查不到单价 ⇒ 内核拒绝在不知道价格时提交。先确认网关可达与密钥权限；"
       "⛔ 不许靠调高预算绕过"),

    # ── 规格/文件类（要知道是"谁挂的"才能选对文件）────────────────────────
    _r("bad_spec", KIND_FIX, COST_LOW,
       "规格/决策文件写错 ⇒ 回**产出该文件的子代理**改文件后重跑对应阶段"
       "（这是确定性重跑，不是重做一遍专业判断）",
       by_node={
           S.APPLY_CUT: S.PLAN_CUT,
           S.BUILD_SRT: S.PLAN_SUBTITLES,
           S.ASSEMBLE_BASE: S.PLAN_FINAL,
           S.MIX_BGM: S.PLAN_FINAL,
           S.APPLY_OVERLAY: S.PLAN_FINAL,
           S.GENERATE_BGM: S.PLAN_FINAL,
       }),
    _r("missing_file", KIND_FIX, COST_LOW,
       "决策/规格文件不存在 ⇒ 回对应子代理先产出该文件",
       by_node={
           S.APPLY_CUT: S.PLAN_CUT,
           S.BUILD_SRT: S.PLAN_SUBTITLES,
           S.ASSEMBLE_BASE: S.PLAN_FINAL,
           S.MIX_BGM: S.PLAN_FINAL,
           S.GENERATE_BGM: S.PLAN_FINAL,
       }),
    _r("missing_input", KIND_FIX, COST_LOW,
       "前置产物缺失 ⇒ 回**产出它**的那个阶段重跑",
       by_node={
           S.BURN_TEST: S.APPLY_CUT,
           S.BURN_FULL: S.APPLY_CUT,
           S.ASSEMBLE_BASE: S.BURN_FULL,
           S.MIX_BGM: S.ASSEMBLE_BASE,
           S.APPLY_OVERLAY: S.MIX_BGM,
           S.RUN_QC: S.APPLY_OVERLAY,
       }),
    _r("missing_srt", KIND_FIX, COST_LOW,
       "字幕文件缺失 ⇒ 回字幕制作重跑 kb_srt",
       by_node={S.BURN_TEST: S.BUILD_SRT, S.BURN_FULL: S.BUILD_SRT}),

    # ── 探针 / 环境类：一律停下升级人工 ───────────────────────────────────
    _r("qc_probe_failed", KIND_HUMAN, COST_FREE,
       "质检探针没返回可解析结果（含 A/V 轨级 duration 缺失、抓帧失败、"
       "volumedetect/ebur128 无输出）⇒ **不得当作通过**：先修探针再重跑 kb_qc，"
       "⛔ 不要据此回退任何子代理"),
    _r("missing_stream", KIND_HUMAN, COST_FREE,
       "缺流 ⇒ **停下升级人工**，禁止自动重试"),
    _r("spec_mismatch", KIND_HUMAN, COST_FREE,
       "规格不符 ⇒ **停下升级人工**，禁止自动重试"),
    _r("timeout", KIND_RESUME, COST_FREE,
       "超过该阶段上限被杀（已连同 ffmpeg 后代一起终止）⇒ 重试同一阶段；"
       "屡次超时说明该阶段需要更长的上限或该拆后台，别硬顶着轮"),
    _r("project_outside_root", KIND_HUMAN, COST_FREE,
       "项目不在 projects_root 之内 ⇒ 停下（改 KB_PROJECTS 或把项目放回根下）。"
       "⚠️ 这不是\"重做一遍\""),
    _r("kernel_missing", KIND_HUMAN, COST_FREE,
       "找不到 kb.py ⇒ 停下升级人工（设 KB_ENTRY）"),
    _r("python_missing", KIND_HUMAN, COST_FREE,
       "找不到内核解释器 ⇒ 停下升级人工（设 KB_PYTHON）"),
    _r("override_reason_required", KIND_HUMAN, COST_FREE,
       "调 kb_override 时理由不足 10 字 ⇒ 补上理由再调（**例外必须是一次有记录的决定**）"),
    _r("kernel_exception", KIND_HUMAN, COST_FREE,
       "内核代码/环境异常（**不该出现**；出现即 bug）⇒ 停下升级人工，附 traceback_tail"),
    _r("kernel_crashed", KIND_HUMAN, COST_FREE,
       "内核未返回可解析的 JSON ⇒ 多为环境问题（Python/ffmpeg 缺失）⇒ 升级人工"),
    _r("kernel_business_failure", KIND_HUMAN, COST_FREE,
       "内核以退出码 2 失败但没给 cause ⇒ 无法路由，停下让人看现场"),
    _r("report_exit_conflict", KIND_HUMAN, COST_FREE,
       "报告写 ok=true 但退出码非 0（自相矛盾）⇒ 按失败处理并停下 —— 这正是"
       "\"静默出片\"的形态，必须响亮地暴露"),
    _r("spawn_failed", KIND_HUMAN, COST_FREE,
       "连子进程都没起来 ⇒ 环境问题，升级人工"),
    _r("aborted", KIND_HUMAN, COST_FREE,
       "调用被中止（用户打断）⇒ 用 kb_status 看断点后按序续跑"),
    _r("unknown_failure", KIND_HUMAN, COST_FREE,
       "内核失败但未给 cause ⇒ 无法路由，停下让人看现场"),

    # ── 编排层自身的失败（不是内核的）────────────────────────────────────
    _r("llm_unavailable", KIND_HUMAN, COST_FREE,
       "模型侧不可用（缺密钥 / 网络 / 鉴权 / 限流，重试也没用）⇒ 停下升级人工。"
       "⚠️ 重试同一个必然失败的调用只是白烧钱"),
    _r("llm_bad_output", KIND_RESUME, COST_FREE,
       "模型输出不是约定的 JSON ⇒ **原地重问一次**（不消耗回退预算，但受"
       "\"同因连续 2 次即停\"限制）。两次都不行说明提示词或模型选型有问题，该人看"),
])


def lookup(cause: str) -> Route | None:
    return MATRIX.get(cause)


# ============================================================================
#  裁决结果
# ============================================================================
@dataclass(frozen=True)
class Decision:
    goto: str                     # 下一个节点（HUMAN 表示停下）
    kind: str
    reason: str
    cause: str | None = None
    cost: str = COST_FREE
    consume_retake: bool = False
    cross_agent: bool = False
    needs_approval: bool = False
    approval_key: str = ""
    # 用户同意之后要回退到哪（只有 needs_approval 时有意义）
    resume_target: str | None = None
    halt: bool = False
    note: str = ""


def _halt(node: str, reason: str, *, cause: str | None = None, kind: str = KIND_HUMAN,
          cost: str = COST_FREE, note: str = "") -> Decision:
    return Decision(goto=HUMAN, kind=kind, reason=reason, cause=cause, cost=cost,
                    halt=True, note=note or reason)


def _guard_repeat(
    node: str, cause: str, kind: str, cost: str,
    state: PipelineState, config: AppConfig,
) -> Decision | None:
    """同一 cause 连续 N 次 ⇒ 停。

    ⚠️ 这是**唯一实现**：矩阵入口（`decide`）与评审指定入口（`decide_manual`）共用它。
    若各写一份，"评审指定"那条路就会悄悄绕过"不要第三次重试"这条护栏。
    """
    seen = consecutive_same_cause(state, cause)
    if seen + 1 >= config.budgets.same_cause_limit:
        return _halt(node, f"同一 cause（{cause}）连续 {seen + 1} 次未解决 ⇒ "
                           "按护栏停止重试，向用户报告（不要第三次重试）",
                     cause=cause, kind=kind, cost=cost)
    return None


def _guard_retake(
    node: str, target: str, cause: str, kind: str, cost: str,
    state: PipelineState, config: AppConfig,
) -> Decision | None:
    """单阶段累计回退上限（同一实现，两个入口共用）。"""
    used = retakes_of(state, target)
    if used >= config.budgets.retake_budget:
        return _halt(node, f"{target} 的累计回退已达上限 {config.budgets.retake_budget} 次"
                           f"（最新 cause={cause}）⇒ 停止重试并向用户报告",
                     cause=cause, kind=kind, cost=cost)
    return None


def decide(
    *, node: str, result: KernelResult, state: PipelineState, config: AppConfig
) -> Decision:
    """按矩阵与预算，决定"这一步之后去哪"。

    纯函数（只读 state）⇒ 可以用构造出来的状态直接测，不需要真跑内核。
    """
    budgets = config.budgets

    # ── 1) 通过：走正常后继 ──────────────────────────────────────────────
    if result.ok:
        nxt = S.FOLLOWING.get(node)
        if nxt is None:
            return _halt(node, f"{node} 没有登记后继节点（stages.FOLLOWING 缺项）",
                         kind=KIND_HUMAN)
        return Decision(goto=nxt, kind=KIND_OK, reason=f"{node} 通过 → {nxt}")

    # ── 2) 失败：挑第一个**有明确路由**的 cause ──────────────────────────
    chosen: Route | None = None
    cause: str | None = None
    for c in result.causes or ((result.cause,) if result.cause else ()):
        route = MATRIX.get(c)
        if route is not None:
            chosen, cause = route, c
            break
    if chosen is None or cause is None:
        c = result.cause or "unknown_failure"
        return _halt(node, f"{node} 失败（cause={c}）但回退矩阵里没有这条 cause ⇒ "
                           "无法路由，停下让人看现场（这份矩阵是唯一裁决依据）",
                     cause=c)

    target = chosen.target_for(node)

    # ── 3) 预算护栏（顺序有讲究：先"问人"类，再"次数"类）────────────────
    # 3a) 更贵的动作必须先问用户 —— 红线第 8 条，**先于任何次数检查**。
    #     ⚠️ 目标是**独立的审批节点**而不是 HALT：用户可能同意，那就得继续回退。
    if chosen.expensive and not (state.get("goal") or {}).get("allow_expensive"):
        return Decision(
            goto=S.APPROVAL, kind=chosen.kind,
            reason=(f"{node} 因 {cause} 需要一次**更贵的动作**（{chosen.note}）。"
                    "按红线：偏离先报批 —— 列选项、等用户拍板"),
            cause=cause, cost=chosen.cost, needs_approval=True,
            approval_key=f"expensive:{cause}", resume_target=target,
            note=f"{cause} 需要用户先拍板（更贵的动作）",
        )

    # 3b) 跨 agent 回路：每片 ≤1 次（**先于**通用"同因连续"检查）
    #     ⚠️ 顺序有讲究：跨 agent 回路是更贵、更特殊的一条护栏，它的理由（代价说明）
    #     对用户有用得多。若让通用的"同因连续"先命中，用户只会看到
    #     "同一 cause 连续 2 次"，完全看不出背后是"改保留块会让字幕全废"这件事。
    if chosen.cross_agent and cross_agent_count(state) >= budgets.cross_agent_loop_budget:
        return _halt(node, f"跨 agent 回路已用满 {budgets.cross_agent_loop_budget} 次"
                           f"（{cause}）⇒ 按护栏停下报告用户。代价说明：改保留块会让"
                           "字幕全部失效、edited_final.mp4 需重新编码，比工具级重跑贵一个数量级",
                     cause=cause, kind=KIND_AGENT_LOOP, cost=COST_HIGH)

    # 3c) 同一 cause 连续 N 次未解决 ⇒ 停（契约：不是第三次重试）
    blocked = _guard_repeat(node, cause, chosen.kind, chosen.cost, state, config)
    if blocked is not None:
        return blocked

    # 3d) "不是错误"的分支：不消耗预算
    if chosen.kind == KIND_BENIGN:
        goto = target or S.FOLLOWING.get(node) or S.DELIVER
        return Decision(goto=goto, kind=KIND_BENIGN, reason=f"{cause}：{chosen.note}",
                        cause=cause, cost=COST_FREE, note=f"{cause}：{chosen.note}")

    # 3d) 续轮询 / 同阶段重试：不消耗回退预算，但仍受"同因连续"限制（否则会无限轮）
    #     ⚠️ 目标缺省是**它自己** —— `timeout` 的语义就是"这一步再跑一次"；
    #     只有续轮询（`bgm_timeout`）才需要显式写同一个付费节点。
    if chosen.kind == KIND_RESUME:
        goto = target or node
        return Decision(goto=goto, kind=KIND_RESUME,
                        reason=f"{cause}：重试同一阶段 {goto}（{chosen.note}）",
                        cause=cause, cost=chosen.cost,
                        note=f"{cause}：{chosen.note}")

    if target is None or target == HUMAN:
        return _halt(node, f"{node} 因 {cause} 无法自动修复（{chosen.note}）",
                     cause=cause, kind=chosen.kind, cost=chosen.cost)

    # 3e) 单阶段累计回退上限
    consume = chosen.kind in (KIND_FIX, KIND_FIX_PARAM, KIND_RETHINK)
    if consume:
        blocked = _guard_retake(node, target, cause, chosen.kind, chosen.cost, state, config)
        if blocked is not None:
            return blocked

    return Decision(
        goto=target, kind=chosen.kind,
        reason=f"{cause} ⇒ 回退到 {target}（{S.owner_of(target)}）：{chosen.note}",
        cause=cause, cost=chosen.cost,
        consume_retake=consume,
        cross_agent=chosen.cross_agent,
        note=f"{cause} ⇒ 回退 {target}（第 {retakes_of(state, target) + 1} 次）",
    )


def decide_manual(
    *, node: str, target: str, cause: str, reason: str, state: PipelineState,
    config: AppConfig, kind: str = KIND_RETHINK, cost: str = COST_MID,
    consume_retake: bool = True,
) -> Decision:
    """**由评审指定的**回退：目标来自成片质检的判断，而不是查表。

    成片质检的职责就是"给回退目标"（contracts §五），所以它必须能指定节点。
    但它**不能自己直接 goto** —— 那样"评审指定"会变成绕过预算的后门
    （同因连续、单阶段上限、跨 agent 三条护栏全部失效）。

    所以：目标先校验（必须是真实工作节点，不许指向 HALT/APPROVAL 这类控制节点），
    再过与矩阵入口**同一份**护栏实现。
    """
    if target not in S.WORKER_NODES:
        return _halt(node, f"评审给出的回退目标不合法：{target!r}"
                           "（必须是真实的工作节点，不能是控制节点）", cause=cause)
    if target == node:
        # 回退到自己 = "同一个判断再想一次" ⇒ 按重试处理（不消耗回退预算，但仍受同因限制）
        blocked = _guard_repeat(node, cause, kind, cost, state, config)
        if blocked is not None:
            return blocked
        return Decision(goto=node, kind=KIND_RESUME, reason=reason, cause=cause,
                        cost=cost, note=reason)

    blocked = _guard_repeat(node, cause, kind, cost, state, config)
    if blocked is not None:
        return blocked
    if consume_retake:
        blocked = _guard_retake(node, target, cause, kind, cost, state, config)
        if blocked is not None:
            return blocked
    return Decision(goto=target, kind=kind, reason=reason, cause=cause, cost=cost,
                    consume_retake=consume_retake, note=reason)


def advance(
    *, node: str, result: KernelResult, state: PipelineState, config: AppConfig,
    decision: Decision | None = None,
) -> tuple[str, dict]:
    """`decide()` + 产出**状态更新**。节点们只调这一个函数，不各自记账。

    `decision` 用于"评审已自行裁决"的情形（见 `decide_manual`）：
    裁决由评审给出，但**记账与其他后果（预算、流水、停下事件）仍然只有这一份实现**。
    """
    d = decision or decide(node=node, result=result, state=state, config=config)

    update: dict = {"results": {node: _to_dict(result)}}
    if not result.ok:
        causes = list(result.causes) or [result.cause or "unknown_failure"]
        update["cause_history"] = causes
    if d.consume_retake:
        update["retakes"] = {d.goto: retakes_of(state, d.goto) + 1}
    if d.cross_agent:
        update["cross_agent_events"] = [{"node": node, "cause": d.cause,
                                         "goto": d.goto, "cost": COST_HIGH}]
    update["route_history"] = [{
        "node": node, "ok": result.ok, "causes": list(result.causes),
        "goto": d.goto, "kind": d.kind, "cost": d.cost, "reason": d.reason,
    }]
    if d.needs_approval:
        # 待办写进状态：APPROVAL 节点与"提出请求的节点"是两次独立执行，
        # 中间隔着一次 interrupt，靠局部变量传不过去。
        update["pending_approval"] = {
            "from_node": node, "kind": d.kind, "approval_key": d.approval_key,
            "cause": d.cause, "reason": d.reason, "resume_target": d.resume_target,
        }
    if d.halt:
        update["halts"] = [{"node": node, "cause": d.cause, "reason": d.reason,
                            "needs_approval": d.needs_approval,
                            "approval_key": d.approval_key}]
    if d.note:
        update["notes"] = [d.note]
    return d.goto, update


def graph_target(goto: str) -> str:
    """把裁决的 `goto` 翻译成**图节点名**。

    `HUMAN` 不是图节点（它表示"停下、升级人工"），要落到 `HALT` 去写报告。
    这个一行的函数存在的意义是：让"裁决用 HUMAN、图用 HALT"这处差异**只有一份**，
    而不是散在十几个节点里各写一次（那种散落的映射迟早会漏一个）。
    """
    return S.HALT if goto == HUMAN else goto


def _to_dict(result: KernelResult) -> dict:
    # 复用 state 里的转换，避免"两处各写一份"（那正是本项目最忌讳的漂移）
    from .state import result_to_dict

    return result_to_dict(result)


# ============================================================================
#  交付用：把裁决流水渲染成可读记录（contracts §三 要求交付时给出）
# ============================================================================
def render_route_history(state: PipelineState) -> list[str]:
    lines: list[str] = []
    for i, rec in enumerate(state.get("route_history") or [], start=1):
        if rec.get("ok"):
            continue
        causes = ", ".join(rec.get("causes") or []) or "?"
        lines.append(
            f"{i}. [{causes}] 发生在 {rec.get('node')} → {rec.get('goto')}"
            f"（{rec.get('kind')}/{rec.get('cost')}）：{rec.get('reason')}"
        )
    return lines
