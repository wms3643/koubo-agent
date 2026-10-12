"""图状态模型。

⭐ **状态必须能被 checkpointer 序列化**（我们用 SQLite 存检查点，"断点续跑"靠它）。
所以状态里**只放 JSON 原生类型** —— `KernelResult` 这种 dataclass 一律转成 dict 再存，
读取时再还原。把 dataclass 直接塞进状态，今天能跑、换个序列化器就炸，
属于"埋一个以后才发作的雷"，不值得。

状态分四类字段，职责别混：

    1. **事实**：`status` / `results` / `last` —— 只读的内核报告（判断的原料）
    2. **决策**：`last_node` / `approvals` / `goal` —— 编排做的选择
    3. **账本**：`retakes` / `cross_agent_loops` / `cause_history` —— 护栏预算的依据
    4. **产出**：`notes` / `final` / `halt_reason` —— 交付时要给用户看的东西
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from .kernel import KernelResult
from .stages import PROBE, WORKER_NODES


def merge_dict(left: dict | None, right: dict | None) -> dict:
    """`dict` 合并型 reducer（浅合并，右侧优先）。

    用途：多个节点各自写一部分账本（如 `retakes` / `results`），
    用"合并"而不是"整体替换"，节点就不必先读全量再写回 —— 少一次读改写就少一类竞态。
    """
    out = dict(left or {})
    out.update(right or {})
    return out


class PipelineState(TypedDict, total=False):
    """流水线的全部状态。`total=False`：节点只返回自己改动的键。

    ⚠️ **凡是可能被多个节点写到的键，一律带 reducer**（`merge_dict` / `operator.add`）。
    理由不是洁癖：LangGraph 在同一个 superstep 里并发写同一个"无 reducer"的键会直接
    `InvalidUpdateError`（实测过）。当前实现是单链、不会并发，但只要以后把"字幕线 ∥ 特效线"
    打开，缺 reducer 的键就会立刻炸 —— 那种错误还偏偏只在并行路径上出现，最难查。
    所以**从一开始就按并发安全来写**：
      · 计数类（回退次数 / 跨 agent 回路）→ 存成**列表**，次数 = `len(list)`，
        这样两个分支同时追加也不会互相覆盖；
      · 记录类（cause 流水 / 裁决记录 / 处置说明）→ 追加型列表。
    """

    # ---------------------------------------------------------------- 事实
    project: str                          # 项目绝对路径（唯一入口参数）
    status: dict[str, Any]                # kb_status 报告（断点续跑依据）
    resume_from: str | None               # 本次从哪个节点接续（PROBE 决定）
    results: Annotated[dict[str, dict], merge_dict]   # 节点名 → 该节点最近结果

    # ---------------------------------------------------------------- 决策
    goal: dict[str, Any]                  # 用户目标：是否要动效层、BGM 偏好…
    approvals: Annotated[dict[str, bool], merge_dict]  # 人工闸门的答复（按 key 记录）
    revision: Annotated[dict[str, Any], merge_dict]    # 评审给出的重做参数（如 margin_v）
    # 待用户拍板的事项。写在状态里而不是节点局部变量里 —— 因为 APPROVAL 节点
    # 与"提出请求的节点"是两次独立的执行（中间隔着一次 interrupt）。
    pending_approval: dict[str, Any]

    # ---------------------------------------------------------------- 账本
    retakes: Annotated[dict[str, int], merge_dict]     # 目标节点 → 已回退次数
    cross_agent_events: Annotated[list[dict], operator.add]  # 跨 agent 回路事件（≤1）
    cause_history: Annotated[list[str], operator.add]  # 按时间顺序的 cause 流水
    route_history: Annotated[list[dict], operator.add]  # 每次裁决记录（交付要用）

    # ---------------------------------------------------------------- 产出
    notes: Annotated[list[str], operator.add]          # 给用户的处置说明
    halts: Annotated[list[dict], operator.add]         # 停下的事件（halted = bool(halts)）
    final: dict[str, Any]                 # 交付信息（成片路径 / 规格 / 结论）


def initial_state(
    project: str,
    *,
    goal: dict[str, Any] | None = None,
    resume_from: str | None = None,
) -> PipelineState:
    """新建一次运行的状态。默认 `goal` 表达的是**契约默认行为**（不是"随便选一个"）。"""
    return PipelineState(
        project=project,
        goal={
            # ⛔ 契约：动效层不得默认跳过（只有用户明确要求无动效版才可省略）
            "overlay_required": True,
            # BGM 默认自己生成（约 0.49 元/次）；用户给了曲目就走用户那份
            "bgm_source": "generate",
            # 真正花钱/重转写这类"更贵的动作"是否已获用户授权（默认没有 ⇒ 会中断询问）
            "allow_paid": False,
            "allow_expensive": False,
            **(goal or {}),
        },
        resume_from=resume_from,
        results={},
        retakes={},
        cause_history=[],
        route_history=[],
        cross_agent_events=[],
        notes=[],
        halts=[],
        approvals={},
        revision={},
    )


# ============================================================================
#  KernelResult ↔ dict（跨 checkpointer 边界）
# ============================================================================
# 报告里的长列表最多保留多少条（截断会**留痕**，见 `_trim_report`）
MAX_REPORT_LIST = 24
# 这些键**永不截断**：它们是判读依据（`causes` 直接决定回退目标）
REPORT_KEEP_WHOLE = frozenset({"causes"})


def _trim_report(report: dict) -> dict:
    """把报告里的长列表截断 —— 状态要进 SQLite 并被**每一步**重写。

    ⚠️ 只截列表：标量与字典（`checks` / `stats` / `error`）是判读要用的，动不得；
    列表类（`frames` / `junction_frames` / `segments` / `off_block_segments`）动辄几十上百条，
    全量塞进检查点会让每一步都很重（而检查点是要反复读写的）。

    ⚠️ 截断必须**留痕**（`__truncated__`）：静默截断会让下游以为"就这么多"，
    而这正是本项目最忌讳的一类错误 —— 断言/证据"看起来在、其实被砍掉了"。
    """
    out: dict[str, Any] = {}
    for k, v in (report or {}).items():
        if (isinstance(v, list) and len(v) > MAX_REPORT_LIST
                and k not in REPORT_KEEP_WHOLE):
            out[k] = list(v[:MAX_REPORT_LIST])
            out[f"{k}__truncated__"] = len(v) - MAX_REPORT_LIST
        else:
            out[k] = v
    return out


def result_to_dict(result: KernelResult) -> dict[str, Any]:
    """把内核结果压成 JSON 原生 dict。

    ⚠️ 刻意**丢掉** `stdout_tail` / `stderr_tail` 的完整内容、只留尾部若干字符：
    状态要进 SQLite 并被反复读写，把几十 KB 日志灌进去会让每个检查点都很重。
    排查现场需要完整日志时，去看内核自己写盘的报告（契约也要求以写盘报告为准）。
    """
    return {
        "tool": result.tool,
        "ok": result.ok,
        "cause": result.cause,
        "causes": list(result.causes),
        "exit_code": result.exit_code,
        "report": _trim_report(result.report),
        "duration_s": result.duration_s,
        "timed_out": result.timed_out,
        "spawned": result.spawned,
        "stdout_tail": result.stdout_tail[-600:],
        "stderr_tail": result.stderr_tail[-400:],
        "summary": result.summary,
    }


def result_from_dict(data: dict[str, Any]) -> KernelResult:
    """还原成 `KernelResult`（只用于读判读字段；命令行/进程信息已不可还原，也不必要）。"""
    d = data or {}
    return KernelResult(
        tool=d.get("tool", "?"),
        ok=bool(d.get("ok")),
        cause=d.get("cause"),
        exit_code=d.get("exit_code"),
        report=d.get("report") or {},
        duration_s=float(d.get("duration_s") or 0.0),
        stdout_tail=d.get("stdout_tail") or "",
        stderr_tail=d.get("stderr_tail") or "",
        timed_out=bool(d.get("timed_out")),
        spawned=bool(d.get("spawned", True)),
    )


@dataclass
class Attempt:
    """一次内核调用的记账（在节点内部用，不进状态）。"""

    node: str
    ok: bool
    causes: tuple[str, ...] = ()
    report: dict = field(default_factory=dict)


def count_worker_results(state: PipelineState) -> dict[str, int]:
    """调试/测试用：每个 worker 节点各产出过几次结果。"""
    results = state.get("results") or {}
    return {n: 1 for n in results if n in WORKER_NODES}


def is_fresh(state: PipelineState) -> bool:
    """是否还没跑过任何 worker（只有 PROBE 之前的状态才算"全新"）。"""
    results = state.get("results") or {}
    return not any(n in results for n in WORKER_NODES)


# ============================================================================
#  便捷读取器 —— 让节点/路由不必到处 `state.get(...) or {}`
# ============================================================================
def result_of(state: PipelineState, node: str) -> KernelResult | None:
    data = (state.get("results") or {}).get(node)
    return result_from_dict(data) if data else None


def retakes_of(state: PipelineState, node: str) -> int:
    """某节点已回退过的次数。"""
    return int((state.get("retakes") or {}).get(node, 0))


def cross_agent_count(state: PipelineState) -> int:
    """跨 agent 回路已用次数（契约：**每片 ≤1**）。"""
    return len(state.get("cross_agent_events") or [])


def is_halted(state: PipelineState) -> bool:
    return bool(state.get("halts"))


def halt_reasons(state: PipelineState) -> list[str]:
    return [h.get("reason", "") for h in (state.get("halts") or [])]


def consecutive_same_cause(state: PipelineState, cause: str) -> int:
    """`cause` 在流水末尾**连续**出现了几次。

    ⚠️ 契约要求的是"同一 cause **连续** 2 次未解决就停下"，不是"累计 2 次"：
    同一个 cause 前后隔了别的成功步骤再出现（例如先修好了 SRT 又因为别的原因复发），
    那是一次新问题，不该被历史计数误判成"老问题没解决"。
    """
    count = 0
    for c in reversed(state.get("cause_history") or []):
        if c != cause:
            break
        count += 1
    return count


def note(state: PipelineState, text: str) -> list[str]:
    """追加一条处置说明（返回 list 以配合 `operator.add` reducer）。"""
    return [text]


def feedback_for(state: PipelineState, node: str) -> dict | None:
    """上一次的**失败反馈** —— 仅当"这次执行是因为回退到本节点"时返回。

    ⚠️ 为什么它是本应用最要紧的一块：没有反馈的重试，等于让模型**把同一个错误答案
    再答一遍**（提示词一模一样、证据一模一样、温度也一样 ⇒ 结果大概率也一样）。
    那回退循环就纯粹是在烧 token 与时间，而"同一 cause 连续 2 次即停"这条护栏
    会很快把它掐掉 —— 于是整条流水线在第一次判断失误后就死了。

    契约里子代理本来就会收到 cause 与提示（`agents/director.md`：子代理报 cause，
    主管复核后再裁决）；DSH 版靠子代理自己读到工具返回值。LangGraph 版里模型**看不到**
    工具返回值，所以编排层必须把失败证据背回来 —— 这一步不是锦上添花，是回退能工作的前提。
    """
    history = state.get("route_history") or []
    if not history:
        return None
    last = history[-1]
    # 只有当"最近一次失败的回退目标就是本节点"时，才有必要喂反馈
    if last.get("ok") or last.get("goto") != node:
        return None
    failed_at = last.get("node")
    report = ((state.get("results") or {}).get(failed_at) or {}).get("report") or {}
    detail = {k: report[k] for k in ("segments", "checks", "off_block_segments")
              if k in report}
    return {
        "failed_at": failed_at,
        "causes": list(last.get("causes") or []),
        "reason": last.get("reason"),
        "error": report.get("error"),
        "hint": report.get("hint"),
        "detail": detail,
        "attempt": retakes_of(state, node),
    }
