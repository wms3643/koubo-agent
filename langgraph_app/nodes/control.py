"""控制节点 —— 主管自己干的四件事：**问状态、请人拍板、交付、停下**。

它们不产生"专业产物"，所以和 worker 节点分开写。其中 `request_approval` 是本应用里
唯一使用 `interrupt()` 的地方，理由见下。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END
from langgraph.types import Command, interrupt

from .. import routing as R
from .. import stages as S
from ..config import AppConfig
from ..kernel import KernelClient
from ..state import PipelineState, halt_reasons, result_to_dict
from .base import NodeFn, command, probe_video, write_json

# 交付报告落盘位置（与内核各阶段的报告并列，便于用户一处翻）
REPORT_NAME = "pipeline_report.json"


def end_command(update: dict) -> Command:
    """终态节点（交付 / 停下）返回它。

    ⚠️ 不能复用 `command()`：那个函数会把 `HUMAN` 翻成 `HALT`，而终态要落到图的 `END`。
    早先我图省事写成 `command(S.DELIVER, ...)`，那其实是**自环** —— 交付节点会把自己
    再触发一遍，转个没完。终态与"路由到某节点"是两件事，值得两个函数。
    """
    return Command(goto=END, update=update)


def make_probe(client: KernelClient, config: AppConfig) -> NodeFn:
    """`kb_status` —— **先问状态，再动**（断点续跑的唯一依据，不许凭记忆猜）。"""

    def probe(state: PipelineState):
        project = Path(state["project"])
        result = client.status(project)

        if not result.ok:
            goto, update = R.advance(node=S.PROBE, result=result, state=state, config=config)
            return command(goto, update)

        report = result.report or {}
        next_stage = report.get("next_stage")
        why = f"next_stage={next_stage!r}"
        nxt = S.resume_node(next_stage)

        # ⚠️ 「产物齐了」**不等于**「质检过了」。
        #    `kb_status` 判 `6_qc` 完成只看 `qc_report.json` **在不在**，所以一次
        #    **未通过**的质检也会被算成 `done` ⇒ 直接交付。真跑撞到过：一个带
        #    `bgm_silent`（BGM 等于没进去）的项目 `next_stage='done'`，照它走就会
        #    把有问题的成片交出去，而问题被完全盖住。
        #    正确做法：`done` 以 `last_qc_ok` 为准；未通过就按质检报出来的 cause
        #    查**回退矩阵**，回到该修的那一步（矩阵是唯一裁决依据，不另发明映射）。
        if next_stage == "done" and report.get("last_qc_ok") is not True:
            causes = list(report.get("last_qc_causes") or [])
            for cause in causes:
                route = R.MATRIX.get(cause)
                target = route.target_for(S.RUN_QC) if route is not None else None
                if target and target in S.WORKER_NODES:
                    nxt = target
                    why += (f"，但 last_qc_ok={report.get('last_qc_ok')}"
                            f"（cause={cause}）⇒ 不算完成，回到 {nxt}")
                    break
            else:
                nxt = S.RUN_QC
                why += (f"，但 last_qc_ok={report.get('last_qc_ok')}"
                        f"（cause={causes or '未给出'}）⇒ 不算完成，先重新质检一次")

        update = {
            "results": {S.PROBE: result_to_dict(result)},
            "status": report,
            "resume_from": nxt,
            "route_history": [{
                "node": S.PROBE, "ok": True, "causes": [],
                "goto": nxt, "kind": R.KIND_OK, "cost": R.COST_FREE,
                "reason": f"断点 {why} ⇒ 从 {nxt}（{S.owner_of(nxt)}）接续",
            }],
            "notes": [f"kb_status：{why} ⇒ 从 {nxt} 接续"],
        }
        # ⚠️ 先过一道路由到 `OVERRIDE`（而不是直接去续跑目标）：
        #     例外放行（重转写等）会**改变续跑的意义**，必须紧跟在"问清状态"之后、
        #     "开始干活"之前，否则会出现"知道要重转写却已经用旧逐字稿跑了一半"。
        return command(S.OVERRIDE, update)

    return probe


def make_override(client: KernelClient, config: AppConfig) -> NodeFn:
    """执行**例外放行**（`kb_override`）—— 主管独有，且必须写明理由。

    为什么需要它存在：`kb_override` 是本项目里唯一承载"偏离红线例外"的手段
    （重转写 `--force`、放行删幻觉段）。契约规定它只给主管，所以编排层必须能发起它；
    否则那两个开关就是**不可达的代码**，而"文档承诺了、代码做不到"正是本项目最忌讳的
    一类问题（我在内核里刚修过一个同类）。

    三个硬约束（都落在代码里）：
      · **理由 ≥10 字** —— 由 `KernelClient` 在调用前拦（`override_reason_required`）。
        例外必须是一次**有记录的决定**，不是形式主义；
      · **默认不用** —— 只有用户在命令行显式指定 action + reason 才会触发；
      · **续跑点跟着前移** —— 见 `stages.OVERRIDE_RESUME`，否则会拿作废的产物继续跑。
    """

    def override(state: PipelineState):
        goal = state.get("goal") or {}
        pending = goal.get("override") or {}
        action = str(pending.get("action") or "").strip()
        resume_target = state.get("resume_from") or S.TRANSCRIBE

        if not action:
            # 没有例外要放行 ⇒ 直接走续跑目标（本节点是透明的）
            return command(resume_target, {})

        if action not in S.OVERRIDE_RESUME:
            return command(S.HALT, {"halts": [{
                "node": S.OVERRIDE, "cause": "bad_spec",
                "reason": f"未知的例外动作：{action!r}；"
                          f"只支持 {sorted(S.OVERRIDE_RESUME)}",
            }]})

        project = Path(state["project"])
        reason = str(pending.get("reason") or "")
        if action == S.OVERRIDE_RETRANSCRIBE:
            result = client.override_retranscribe(project, reason=reason)
        else:
            result = client.override_allow_hallucination_cut(project, reason=reason)

        if not result.ok:
            # 失败（如理由太短 → override_reason_required）走矩阵，与别处一致
            goto, update = R.advance(node=S.OVERRIDE, result=result,
                                     state=state, config=config)
            update["resume_from"] = resume_target
            return command(goto, update)

        nxt = S.OVERRIDE_RESUME[action]
        return command(nxt, {
            "results": {S.OVERRIDE: result_to_dict(result)},
            "resume_from": nxt,
            "route_history": [{
                "node": S.OVERRIDE, "ok": True, "causes": [], "goto": nxt,
                "kind": "override", "cost": R.COST_HIGH,
                "reason": f"例外放行 {action}（理由：{reason}）⇒ 续跑点前移到 {nxt}",
            }],
            "notes": [f"⛔ 例外放行 {action}：{reason}（续跑点前移到 {nxt}）"],
        })

    return override


def make_approval(config: AppConfig) -> NodeFn:
    """请用户拍板（付费 / 更贵的动作）—— **唯一使用 `interrupt()` 的节点**。

    ⚠️ 为什么必须单独一个节点：LangGraph 恢复执行时会把节点函数**从头重跑**
    （`interrupt()` 的返回值是那次重跑才拿到的）。若把"请人拍板"写在普通节点里
    （例如"跑完 mix → 发现要重渲 → 询问"），用户一点头，那个昂贵的编码就会被**再跑一遍**。
    本节点从头到尾只有**读操作**，重跑无副作用 —— 这才是安全的落点。
    """

    def request_approval(state: PipelineState):
        pending = state.get("pending_approval") or {}
        if not pending:
            return command(S.HALT, {"halts": [{
                "node": S.APPROVAL, "cause": "internal_error",
                "reason": "收到审批请求，但状态里没有待办事项（内部错误）",
            }]})

        answer = interrupt({
            "type": "approval_request",
            "approval_key": pending.get("approval_key"),
            "from_node": pending.get("from_node"),
            "cause": pending.get("cause"),
            "question": pending.get("reason"),
            "price": pending.get("price"),
            "options": ["approve", "reject"],
        })

        key = pending.get("approval_key") or "unknown"
        target = pending.get("resume_target") or S.HALT

        if not is_approved(answer):
            return command(S.HALT, {"halts": [{
                "node": S.APPROVAL, "cause": pending.get("cause"),
                "reason": f"用户**拒绝**了 {key}：{pending.get('reason')}",
            }], "pending_approval": {}})

        # 授权：按 key 打开对应开关（付费与"更贵的动作"是两个独立授权）
        goal = dict(state.get("goal") or {})
        if key.startswith("paid:"):
            goal["allow_paid"] = True
        if key.startswith("expensive:"):
            goal["allow_expensive"] = True
        return command(target, {
            "goal": goal,
            "approvals": {key: True},
            "pending_approval": {},
            "notes": [f"用户批准了 {key} ⇒ 继续到 {target}"],
        })

    return request_approval


def is_approved(answer) -> bool:
    """把"用户的答复"鲁棒地翻译成布尔。

    为什么要兼容这么多种形态：这个值可能来自 CLI、来自脚本、来自人工拼的 JSON，
    形态不受我们控制。**判错方向的代价不对称** —— 把"拒绝"误判成"同意"会直接花钱、
    或重跑一次几十分钟的编码；反过来最多多问一次。所以这里**只认明确的同意**。
    """
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, str):
        return answer.strip().lower() in ("approve", "approved", "yes", "y", "同意", "批准", "可以", "ok")
    if isinstance(answer, dict):
        for key in ("approve", "approved", "allow", "ok", "同意"):
            if key in answer:
                return is_approved(answer[key])
        # 形如 {"decision": "approve"} 的形态
        for key in ("decision", "answer", "choice"):
            if key in answer:
                return is_approved(answer[key])
    return False


def make_deliver(config: AppConfig) -> NodeFn:
    """交付：一次性给出**成片路径 + ffprobe 规格 + 每个 cause 的处理记录**。

    契约明确要求不能只说"已完成" —— 用户要能自己复核（`agents/director.md` 交付口径）。
    """

    def deliver(state: PipelineState):
        project = Path(state["project"])
        final = project / "renders" / "final.mp4"
        assembled = project / "renders" / "assembled.mp4"
        spec = probe_video(final) if final.is_file() else {}
        goal = state.get("goal") or {}

        # ⚠️ "动效层进没进成片"要看**文件在不在**，不是看"节点跑没跑"。
        #    节点跑过但渲染被跳过（用户要无动效版）时，判"已包含"就是在交付说明里说谎。
        overlay_dir = project / "overlays" / "renders"
        overlay_files = sorted(overlay_dir.glob("*.mov")) if overlay_dir.is_dir() else []

        info = {
            "project": str(project),
            # 主交付物与备选（无特效版）
            "final_video": str(final) if final.is_file() else None,
            "final_exists": final.is_file(),
            "assembled_backup": str(assembled) if assembled.is_file() else None,
            "spec": spec,
            # 动效层是否真的进了成片（契约：它不得默认跳过）
            "overlay_included": bool(overlay_files),
            "overlay_files": [str(p) for p in overlay_files],
            "overlay_required": bool(goal.get("overlay_required", True)),
            # 每个 cause 的处理记录 —— 矩阵数据化的自然产出
            "cause_history": list(state.get("cause_history") or []),
            "fallbacks": R.render_route_history(state),
            "notes": list(state.get("notes") or []),
            "approvals": dict(state.get("approvals") or {}),
            "known_gaps": _known_gaps(state, final),
        }
        # 首次交付说明：契约要求"未含动效层"之类必须写明
        if not info["overlay_included"]:
            info.setdefault("disclaimer", [])
            info["disclaimer"].append("本片**未含动效层**（未执行叠加步骤）")

        write_json(project / "edit" / REPORT_NAME, info)
        return end_command({"final": info, "notes": [f"交付报告已写入 edit/{REPORT_NAME}"]})

    return deliver


def _failing_details(state: PipelineState) -> dict:
    """把各阶段失败时的**现场**整理出来（错误、栈尾、子进程输出尾巴）。

    只说"某阶段 kernel_exception"是没用的 —— 排查的人需要的是那一行报错。
    """
    out: dict[str, dict] = {}
    for node, data in (state.get("results") or {}).items():
        if data.get("ok") or not data.get("cause"):
            continue
        report = data.get("report") or {}
        out[node] = {
            "cause": data.get("cause"),
            "error": report.get("error"),
            "hint": report.get("hint"),
            "traceback_tail": report.get("traceback_tail"),
            "stdout_tail": data.get("stdout_tail"),
            "stderr_tail": data.get("stderr_tail"),
        }
    return out


def _known_gaps(state: PipelineState, final: Path) -> list[str]:
    """列出"已知未解决项"（契约要求：若有，说明为什么）。"""
    gaps: list[str] = []
    if not final.is_file():
        gaps.append("成片 renders/final.mp4 不存在")
    results = state.get("results") or {}
    if S.RUN_QC not in results:
        gaps.append("未执行片尾质检（kb_qc target=final）")
    if S.REVIEW_FINAL not in results:
        gaps.append("未执行成片质检的视觉复核")
    return gaps


def make_halt(config: AppConfig) -> NodeFn:
    """停下并**如实报告** —— 把"为什么停、卡在哪、还差什么"写成文件留给用户。"""

    def halt(state: PipelineState):
        project = Path(state["project"])
        reasons = halt_reasons(state)
        info = {
            "halted": True,
            "project": str(project),
            "reasons": reasons,
            "last_halts": list(state.get("halts") or []),
            "cause_history": list(state.get("cause_history") or []),
            "fallbacks": R.render_route_history(state),
            "notes": list(state.get("notes") or []),
            "approvals": dict(state.get("approvals") or {}),
            "pending_approval": dict(state.get("pending_approval") or {}),
            # ⚠️ 把**失败阶段自己的报告**一并带上。内核崩了（exit 1）时真正的原因在
            #    `traceback_tail` 里，而它此前只存在于状态库中 —— 用户得去翻 SQLite
            #    或者手动重跑内核才能看到。报告应当自足（真跑撞到过：halted=true
            #    只写了一句"kernel_exception 无法自动修复"，真正原因一个字都没有）。
            "failing_stages": _failing_details(state),
            "next": ("请人工介入：按上面的 reasons 处理后再用同一 thread_id 续跑"
                     "（已完成的阶段会被跳过，不会重跑）"),
        }
        write_json(project / "edit" / REPORT_NAME, info)
        return end_command({"final": info,
                            "notes": [f"已停下；原因与建议写入 edit/{REPORT_NAME}"]})

    return halt
