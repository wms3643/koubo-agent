"""确定性节点 —— 每个节点 = **一次内核调用**。

这些节点里没有 LLM、没有判断，只有"把参数传对、把结果记账"。判断性的活
（该删哪些段、字幕怎么写、动效怎么做、能不能交付）在 `llm_nodes.py` 里，
两者通过**文件**交接（decisions / fixes / index.html / 评审结论），
与契约里"文件是唯一交接面"的原则一致。
"""

from __future__ import annotations

import json

from .. import stages as S
from ..config import AppConfig
from ..kernel import KernelClient
from ..state import PipelineState
from .base import (
    NodeFn, command, kernel_step, project_of, revision_of, skip, synthetic, write_json,
)


# 自备 BGM 允许的扩展名（与内核 mix 的能力一致）
AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"})


def make_kernel_nodes(client: KernelClient, config: AppConfig) -> dict[str, NodeFn]:
    def transcribe(state: PipelineState):
        return kernel_step(
            node=S.TRANSCRIBE, client=client, config=config, state=state,
            call=lambda: client.transcribe(project_of(state)),
        )

    def apply_cut(state: PipelineState):
        # `run=True` 才真正编码。删幻觉嫌疑段会被内核以
        # `hallucination_cut_forbidden` 拒绝 —— 那是有意的，例外只能走 kb_override。
        return kernel_step(
            node=S.APPLY_CUT, client=client, config=config, state=state,
            call=lambda: client.cut(project_of(state), run=True),
        )

    def build_srt(state: PipelineState):
        return kernel_step(
            node=S.BUILD_SRT, client=client, config=config, state=state,
            call=lambda: client.srt(project_of(state)),
        )

    def burn_test(state: PipelineState):
        # `subtitle_occlusion`（字幕挡脸）回退时，评审会把新的 MarginV 放进 revision
        return kernel_step(
            node=S.BURN_TEST, client=client, config=config, state=state,
            call=lambda: client.burn(project_of(state), mode="test",
                                     margin_v=revision_of(state, "margin_v")),
        )

    def burn_full(state: PipelineState):
        return kernel_step(
            node=S.BURN_FULL, client=client, config=config, state=state,
            call=lambda: client.burn(project_of(state), mode="full",
                                     margin_v=revision_of(state, "margin_v")),
        )

    def render_overlay(state: PipelineState):
        # ⚠️ 必须与 design_overlay 的跳过**成对**：只跳设计不跳渲染，等于白跑一次
        #    昂贵的 HyperFrames 渲染（而且渲染的是上一次留下的旧工程）。
        if not (state.get("goal") or {}).get("overlay_required", True):
            return skip(S.RENDER_OVERLAY, state, config,
                        "用户明确要求无动效版 ⇒ 跳过动效渲染")
        return kernel_step(
            node=S.RENDER_OVERLAY, client=client, config=config, state=state,
            call=lambda: client.overlay(project_of(state),
                                        output=revision_of(state, "overlay_output")),
        )

    def generate_bgm(state: PipelineState):
        """BGM —— **唯一会花钱**的一步，也是唯一需要"两步式人工确认"的一步。

        流程（每一步的先后都有理由）：
          ① 用户明确指定了曲目 ⇒ 直接跳过生成，**一分钱不花**；
          ② 未获授权时，先用**免费**调用探一次（不带确认的内核调用只会返回
             `bgm_exists` 或 `bgm_needs_confirm`，**绝不会提交**）；
             · `bgm_exists` ⇒ 已有可用成品，免费复用（这是最常走到的分支）；
             · `bgm_needs_confirm` ⇒ 再免费查一次价，然后**转交审批节点问用户**；
          ③ 已获授权 ⇒ 才带 `confirm_paid=True` 真提交（内核自带"防重复扣费"）。
        """
        project = project_of(state)
        goal = state.get("goal") or {}

        if goal.get("bgm_source") == "user":
            # ⚠️ 必须**先确认用户真的把曲目放进来了**。
            #    否则：本节点跳过 → mix 报 `bgm_missing` → 矩阵回退到**本节点** → 又跳过，
            #    变成死循环（最后被"同因连续 2 次"掐掉，但报出来的理由是
            #    "同一 cause 连续 2 次"，**完全看不出真正的原因**）。
            #    而且这件事没有自动修法：既不能替用户找曲子，也不该偷偷改成"生成"（要花钱）。
            #    ⇒ 归 `missing_user_bgm` → 停下问人。
            found = [p for p in sorted((project / "bgm").glob("*"))
                     if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
            if not found:
                return kernel_step(
                    node=S.GENERATE_BGM, client=client, config=config, state=state,
                    call=lambda: synthetic(
                        S.GENERATE_BGM, ok=False, cause="missing_user_bgm",
                        report={"ok": False, "cause": "missing_user_bgm",
                                "error": f"指定用自备曲目，但 {project / 'bgm'} 下没有音频文件"
                                         f"（支持 {sorted(AUDIO_EXTS)}）—— 把曲目放进去，"
                                         "或改用 --bgm-source generate 让内核生成（约 0.49 元/次）"}))
            return skip(S.GENERATE_BGM, state, config,
                        f"BGM 用你提供的曲目：{found[0].name}（跳过生成，不花钱）")

        if goal.get("allow_paid"):
            result = client.bgm(project, confirm_paid=True)
        else:
            probe = client.bgm(project, confirm_paid=False)
            if probe.ok or probe.cause == "bgm_exists":
                return skip(S.GENERATE_BGM, state, config,
                            f"BGM 已有可用成品 ⇒ 免费复用（{probe.cause or 'ok'}），未重复扣费")
            if probe.cause == "bgm_needs_confirm":
                quote = client.bgm(project, dry_run=True)     # 免费查价
                price = (quote.report or {}).get("price_per_call")
                price_txt = f"约 {price} 元/次" if price is not None else "单价未知"
                return command(S.APPROVAL, {"pending_approval": {
                    "from_node": S.GENERATE_BGM,
                    "kind": "paid",
                    "approval_key": "paid:bgm",
                    "cause": "bgm_needs_confirm",
                    "reason": f"生成 BGM 需要付费（{price_txt}）。内核拒绝在未确认时提交，"
                              "所以要你拍板；已有成品时会免费复用、不会重复扣费。",
                    "resume_target": S.GENERATE_BGM,
                    "price": price,
                }})
            result = probe       # 其它失败 ⇒ 交给矩阵裁决

        return kernel_step(
            node=S.GENERATE_BGM, client=client, config=config, state=state,
            call=lambda: result,
        )

    def assemble_base(state: PipelineState):
        # 缝接骨架：段来自 剪裁策划 写的 edit/assemble.json（章节页/开场）
        return kernel_step(
            node=S.ASSEMBLE_BASE, client=client, config=config, state=state,
            call=lambda: client.assemble(project_of(state), mode="assemble",
                                         spec="edit/assemble.json"),
        )

    def mix_bgm(state: PipelineState):
        # 铺 BGM：video 必须是**带人声**的主轨（mix.json 里由 剪裁策划 指定）
        return kernel_step(
            node=S.MIX_BGM, client=client, config=config, state=state,
            call=lambda: client.assemble(project_of(state), mode="mix",
                                         spec="edit/mix.json"),
        )

    def apply_overlay(state: PipelineState):
        """叠加动效层 → **产出成片** `renders/final.mp4`。

        ⚠️ 这一步的规格由**代码**决定，不让 LLM 猜路径。理由是真跑暴露的两个后果：

          ① 契约只说"剪裁策划给 segments、特效设计加 overlays"，**没说清最终输出叫什么**。
             没人把 `output` 改成 `renders/final.mp4` ⇒ **成片永远不产出**，
             紧接着 `kb_qc(target=final)` 就失败（而且报的是 `missing_input`，指向很偏）。
          ② 若 `segments` 指向混音**前**的视频 ⇒ 成片会**静默没有 BGM** ——
             而"BGM 到底进没进去"只有 `kb_qc` 的数字断言看得出来，肉眼看片不容易发现。

        这两件事都不是"专业判断"，而是**管道一致性**：混哪两个文件、输出到哪、动效层取哪个，
        全是确定性事实。按本项目一贯分工（"确定性计算由代码执行、责任归 agent"），
        归代码。
        """
        project = project_of(state)
        goal = state.get("goal") or {}

        def _fail(cause: str, message: str) -> KernelResult:
            return synthetic(S.APPLY_OVERLAY, ok=False, cause=cause,
                             report={"ok": False, "cause": cause, "error": message})

        spec_path = project / "edit" / "assemble.json"
        if not spec_path.is_file():
            return kernel_step(
                node=S.APPLY_OVERLAY, client=client, config=config, state=state,
                call=lambda: _fail("missing_file",
                                   "edit/assemble.json 不存在（应在上一步产出）"))

        # 动效层：必须有渲染产物，否则按契约报 missing_overlay（矩阵会回退到 kb_overlay）
        overlays: list[dict] = []
        if goal.get("overlay_required", True):
            movs = sorted((project / "overlays" / "renders").glob("*.mov"))
            if not movs:
                return kernel_step(
                    node=S.APPLY_OVERLAY, client=client, config=config, state=state,
                    call=lambda: _fail(
                        "missing_overlay",
                        "要求含动效层，但 overlays/renders 下没有 .mov —— "
                        "说明渲染那步没成功或没产出"))
            overlays = [{"path": str(movs[-1].relative_to(project)).replace("\\", "/")}]

        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
        spec["segments"] = [{"path": S.MIXED_MASTER, "kind": "main"}]
        spec["output"] = S.FINAL_VIDEO
        if overlays:
            spec["overlays"] = overlays
        else:
            spec.pop("overlays", None)
        write_json(spec_path, spec)

        return kernel_step(
            node=S.APPLY_OVERLAY, client=client, config=config, state=state,
            call=lambda: client.assemble(project, mode="assemble",
                                         spec="edit/assemble.json"),
        )

    def run_qc(state: PipelineState):
        # ⚠️ target 必须是 "final"：选错目标会拿到完全不同的检查项集合
        #    （拿全片 SRT 去比 10s 测试片会恒报假 srt_timeline_mismatch）
        return kernel_step(
            node=S.RUN_QC, client=client, config=config, state=state,
            call=lambda: client.qc(project_of(state), target="final"),
        )

    return {
        S.TRANSCRIBE: transcribe,
        S.APPLY_CUT: apply_cut,
        S.BUILD_SRT: build_srt,
        S.BURN_TEST: burn_test,
        S.BURN_FULL: burn_full,
        S.RENDER_OVERLAY: render_overlay,
        S.GENERATE_BGM: generate_bgm,
        S.ASSEMBLE_BASE: assemble_base,
        S.MIX_BGM: mix_bgm,
        S.APPLY_OVERLAY: apply_overlay,
        S.RUN_QC: run_qc,
    }
