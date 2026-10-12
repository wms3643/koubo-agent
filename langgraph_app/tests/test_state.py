"""状态层的小回归 —— 全是纯函数，测起来便宜，但它们影响每一步。

两处最值得守：

    · **报告截断必须留痕**。状态每步都要进 SQLite，长列表得截；但静默截断会让下游
      以为"就这么多"（帧图、越界段列表都是判读依据）。所以截断要写 `__truncated__`，
      而 `causes` 这种直接决定回退目标的键**永不截断**。
    · **失败反馈的取数规则**：只有"这次执行是因为回退到本节点"时才喂，
      否则会把无关失败的证据塞给一个无关的判断（比不喂更糟）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langgraph_app import stages as S  # noqa: E402
from langgraph_app.kernel import KernelResult  # noqa: E402
from langgraph_app.state import (  # noqa: E402
    MAX_REPORT_LIST, consecutive_same_cause, cross_agent_count, feedback_for,
    initial_state, is_halted, result_from_dict, result_to_dict, retakes_of,
)


def test_default_goal_is_the_contract_behaviour():
    """默认值必须表达契约默认：动效层不跳过、BGM 自己生成、**不预先授权花钱**。"""
    st = initial_state("C:/tmp/p")
    assert st["goal"]["overlay_required"] is True
    assert st["goal"]["bgm_source"] == "generate"
    assert st["goal"]["allow_paid"] is False
    assert st["goal"]["allow_expensive"] is False


# ---------------------------------------------------------------- 报告截断
def test_long_report_lists_are_trimmed_with_a_marker():
    r = KernelResult(tool="kb_qc", ok=True, exit_code=0, report={
        "ok": True,
        "frames": [f"f{i}.png" for i in range(100)],
        "junction_frames": [f"j{i}.png" for i in range(5)],
    })
    d = result_to_dict(r)
    assert len(d["report"]["frames"]) == MAX_REPORT_LIST
    assert d["report"]["frames__truncated__"] == 100 - MAX_REPORT_LIST
    assert len(d["report"]["junction_frames"]) == 5
    assert "junction_frames__truncated__" not in d["report"]


def test_causes_are_never_trimmed():
    """`causes` 直接决定回退目标 —— 截断它就可能选错该回退到哪。"""
    r = KernelResult(tool="kb_qc", ok=False, exit_code=2, cause=None,
                     report={"ok": False, "causes": [f"c{i}" for i in range(50)]})
    d = result_to_dict(r)
    assert len(d["report"]["causes"]) == 50
    assert "causes__truncated__" not in d["report"]


def test_result_survives_the_dict_round_trip():
    r = KernelResult(tool="kb_cut", ok=False, cause="bad_spec", exit_code=2,
                     report={"ok": False, "cause": "bad_spec", "error": "x"},
                     duration_s=1.5, stdout_tail="tail", stderr_tail="err",
                     timed_out=False, spawned=True)
    back = result_from_dict(result_to_dict(r))
    assert (back.tool, back.ok, back.cause, back.exit_code) == ("kb_cut", False, "bad_spec", 2)
    assert back.report["error"] == "x"
    assert back.duration_s == 1.5


# ---------------------------------------------------------------- 失败反馈
def _failed_state(**kw):
    st = initial_state("C:/tmp/p")
    st.update(kw)
    return st


def test_feedback_when_failure_routed_here():
    st = _failed_state(
        results={S.APPLY_CUT: {"tool": "kb_cut", "ok": False,
                               "report": {"ok": False, "error": "越界", "hint": "改下标",
                                          "segments": [{"idx": 1}]}}},
        route_history=[{"node": S.APPLY_CUT, "ok": False, "causes": ["index_out_of_range"],
                        "goto": S.PLAN_CUT, "kind": "rethink", "cost": "low",
                        "reason": "回退到剪裁策划"}],
        cause_history=["index_out_of_range"],
        retakes={S.PLAN_CUT: 1},
    )
    fb = feedback_for(st, S.PLAN_CUT)
    assert fb is not None
    assert fb["failed_at"] == S.APPLY_CUT
    assert fb["error"] == "越界" and fb["hint"] == "改下标"
    assert fb["detail"]["segments"] == [{"idx": 1}]
    assert fb["attempt"] == 1


def test_no_feedback_when_the_failure_points_elsewhere():
    st = _failed_state(
        route_history=[{"node": S.RUN_QC, "ok": False, "causes": ["srt_bom"],
                        "goto": S.BUILD_SRT, "kind": "fix", "cost": "low", "reason": "重跑"}],
        cause_history=["srt_bom"],
    )
    assert feedback_for(st, S.PLAN_CUT) is None
    assert feedback_for(st, S.BUILD_SRT) is not None


def test_no_feedback_when_last_step_succeeded():
    st = _failed_state(route_history=[{"node": S.BUILD_SRT, "ok": True, "causes": [],
                                       "goto": S.BURN_TEST, "kind": "ok",
                                       "cost": "free", "reason": "通过"}])
    assert feedback_for(st, S.BURN_TEST) is None


# ---------------------------------------------------------------- 计数
def test_consecutive_same_cause_counts_only_the_tail():
    st = _failed_state(cause_history=["a", "b", "b"])
    assert consecutive_same_cause(st, "b") == 2
    assert consecutive_same_cause(st, "a") == 0


def test_counters_default_to_zero():
    st = initial_state("C:/tmp/p")
    assert retakes_of(st, S.PLAN_CUT) == 0
    assert cross_agent_count(st) == 0
    assert is_halted(st) is False
