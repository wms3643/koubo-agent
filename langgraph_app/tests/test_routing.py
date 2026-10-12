"""回退裁决的回归 —— 这是编排层最该被守住的一层。

两类测试，缺一不可：

    **一、矩阵自身的完整性**（遍历式断言）
        交付时那份"每个 cause 的处理记录"直接来自这张矩阵，所以它必须**没有洞**：
        每条都要有可读说明、每个目标都必须是真实存在的节点、每个节点都必须有后继。
        这类断言只能靠遍历，人眼读不出漏项。

    **二、裁决语义**（逐个分支）
        通过 / 工具级修复 / 回子代理重判 / 续轮询 / 不是错误 / 停下升级人工，
        外加四条预算护栏：跨 agent 回路 ≤1、单阶段回退 ≤5、同因连续 2 次即停、
        更贵的动作先问用户。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from langgraph_app import routing as R  # noqa: E402
from langgraph_app import stages as S  # noqa: E402
from langgraph_app.config import AppConfig, Budgets  # noqa: E402
from langgraph_app.kernel import KernelResult  # noqa: E402
from langgraph_app.state import (  # noqa: E402
    consecutive_same_cause, initial_state,
)

CFG = AppConfig()


def fail(cause: str, *, node_causes=None) -> KernelResult:
    return KernelResult(
        tool="stub", ok=False, cause=cause, exit_code=2,
        report={"ok": False, "cause": cause, "causes": node_causes or []},
    )


def ok() -> KernelResult:
    return KernelResult(tool="stub", ok=True, exit_code=0, report={"ok": True})


def state(**kw):
    st = initial_state("C:/tmp/proj")
    st.update(kw)
    return st


def decide(node: str, result: KernelResult, st=None, cfg=CFG):
    return R.decide(node=node, result=result, state=st or state(), config=cfg)


# ===========================================================================
#  一、矩阵自身的完整性
# ===========================================================================
def test_every_route_has_a_readable_note():
    """每条规则都必须写清"为什么回退到那儿" —— 交付记录直接引用它。"""
    for cause, route in R.MATRIX.items():
        assert cause == route.cause
        assert len(route.note.strip()) >= 8, f"{cause} 的说明太短，交付时说不清"


def test_every_route_target_is_a_real_node():
    """⛔ 目标写错会静默退化成"不知道该回退到哪" —— 必须遍历断言。"""
    known = S.known_nodes()
    for cause, route in R.MATRIX.items():
        targets = []
        if route.target:
            targets.append(route.target)
        targets += list(route.by_node.values())
        targets += [route.fallback]
        for t in targets:
            assert t in (known | {R.HUMAN}), f"{cause} 指向了不存在的节点：{t!r}"


def test_every_kind_is_known():
    kinds = {R.KIND_OK, R.KIND_FIX, R.KIND_FIX_PARAM, R.KIND_RETHINK, R.KIND_RESUME,
             R.KIND_AGENT_LOOP, R.KIND_BENIGN, R.KIND_HUMAN}
    for route in R.MATRIX.values():
        assert route.kind in kinds, f"{route.cause} 的 kind 未登记：{route.kind}"


def test_every_cost_is_known():
    costs = {R.COST_FREE, R.COST_LOW, R.COST_MID, R.COST_HIGH}
    for route in R.MATRIX.values():
        assert route.cost in costs, f"{route.cause} 的 cost 未登记：{route.cost}"


def test_every_worker_has_a_successor():
    """除了 PROBE（它按断点决定去哪）与终态，每个 worker 都必须有后继。"""
    for node in S.WORKER_NODES:
        if node == S.PROBE:
            continue
        assert node in S.FOLLOWING, f"{node} 没有登记后继节点（stages.FOLLOWING 缺项）"


def test_chain_covers_all_nodes_without_cycle():
    # HALT 是终态错误出口，不属于 happy path，所以不在链上
    assert set(S.CHAIN) == set(S.WORKER_NODES) | {S.DELIVER}, "单链必须覆盖全部正常节点"
    seen = set()
    for node in S.CHAIN:
        assert node not in seen, f"链上重复出现 {node}（可能是环）"
        seen.add(node)
    # 链上每一步的后继必须与 FOLLOWING 一致（两处不能各说各话）
    for a, b in zip(S.CHAIN, S.CHAIN[1:]):
        if a in S.FOLLOWING:
            assert S.FOLLOWING[a] == b, f"{a} 的后继在 CHAIN 与 FOLLOWING 里不一致"


def test_resume_map_targets_are_real_nodes():
    for stage_name, node in S.RESUME_MAP.items():
        assert node in S.known_nodes(), f"断点映射 {stage_name} → 不存在的节点 {node}"


# ===========================================================================
#  二、裁决语义
# ===========================================================================
def test_pass_follows_happy_path():
    d = decide(S.BUILD_SRT, ok())
    assert d.kind == R.KIND_OK
    assert d.goto == S.BURN_TEST
    assert not d.halt
    assert not d.consume_retake


def test_node_without_registered_successor_halts_loudly():
    """漏登记后继时必须响亮停下，而不是静默结束（PROBE 是特例：它按断点自己决定去哪）。"""
    d = decide("no_such_node", ok())
    assert d.halt is True and d.goto == R.HUMAN
    assert "后继" in d.reason


def test_srt_defects_are_deterministic_refixes():
    """字幕类问题一律工具级重跑，⛔ 不该回子代理重做判断。"""
    for cause in ("srt_bom", "srt_trailing_symbol", "srt_timeline_overlap"):
        d = decide(S.RUN_QC, fail(cause, node_causes=[cause]))
        assert d.goto == S.BUILD_SRT, cause
        assert d.kind == R.KIND_FIX
        assert d.consume_retake is True


def test_reviewer_causes_go_back_to_cut_planner():
    for cause in ("srt_empty", "cut_duration_mismatch", "junction_glitch",
                  "audio_clipping", "av_desync", "index_out_of_range",
                  "empty_keep_set", "hallucination_cut_forbidden"):
        d = decide(S.RUN_QC, fail(cause, node_causes=[cause]))
        assert d.goto == S.PLAN_CUT, cause
        assert d.kind in (R.KIND_RETHINK, R.KIND_FIX)


def test_bad_spec_targets_the_file_owner():
    """`bad_spec` 是"谁的文件错了"决定的 —— 这正是 by_node 要解决的问题。"""
    assert decide(S.APPLY_CUT, fail("bad_spec")).goto == S.PLAN_CUT
    assert decide(S.MIX_BGM, fail("bad_spec")).goto == S.PLAN_FINAL
    assert decide(S.ASSEMBLE_BASE, fail("bad_spec")).goto == S.PLAN_FINAL
    assert decide(S.BUILD_SRT, fail("bad_spec")).goto == S.PLAN_SUBTITLES


def test_bad_spec_from_unknown_node_halts():
    """不知道是哪个文件写错时，宁可停下问人，也不要瞎指一个节点。"""
    d = decide(S.PROBE, fail("bad_spec"))
    assert d.halt is True


def test_missing_input_targets_the_producer():
    assert decide(S.BURN_FULL, fail("missing_input")).goto == S.APPLY_CUT
    assert decide(S.BURN_FULL, fail("missing_srt")).goto == S.BUILD_SRT
    assert decide(S.MIX_BGM, fail("missing_input")).goto == S.ASSEMBLE_BASE
    assert decide(S.RUN_QC, fail("missing_input")).goto == S.APPLY_OVERLAY


def test_subtitle_occlusion_uses_margin_v_retry():
    d = decide(S.REVIEW_TEST, fail("subtitle_occlusion"))
    assert d.goto == S.BURN_TEST
    assert d.kind == R.KIND_FIX_PARAM
    assert "MarginV" in d.reason or "离底" in d.reason


def test_benign_causes_do_not_consume_budget_and_continue():
    d = decide(S.GENERATE_BGM, fail("bgm_exists"))
    assert d.kind == R.KIND_BENIGN
    assert d.goto == S.ASSEMBLE_BASE
    assert d.consume_retake is False
    assert not d.halt

    d2 = decide(S.TRANSCRIBE, fail("transcript_exists"))
    assert d2.goto == S.PLAN_CUT and d2.consume_retake is False


def test_bgm_timeout_is_resume_not_retake():
    d = decide(S.GENERATE_BGM, fail("bgm_timeout"))
    assert d.kind == R.KIND_RESUME
    assert d.goto == S.GENERATE_BGM
    assert d.consume_retake is False, "续轮询不是回退，不该吃回退预算"


def test_timeout_resumes_same_stage():
    d = decide(S.BURN_FULL, fail("timeout"))
    assert d.kind == R.KIND_RESUME and d.goto == S.BURN_FULL


@pytest.mark.parametrize(
    "cause",
    ["missing_stream", "spec_mismatch", "qc_probe_failed", "kernel_exception",
     "kernel_crashed", "bgm_insufficient_balance", "bgm_no_api_key",
     "project_outside_root", "report_exit_conflict", "aborted", "spawn_failed"],
)
def test_human_causes_stop_immediately(cause):
    d = decide(S.RUN_QC, fail(cause, node_causes=[cause]))
    assert d.halt is True, f"{cause} 必须停下升级人工"
    assert d.goto == R.HUMAN


def test_unknown_cause_halts_with_clear_reason():
    d = decide(S.RUN_QC, fail("从来没见过的_cause"))
    assert d.halt is True
    assert "矩阵" in d.reason


def test_multi_cause_picks_the_first_routable_one():
    """`kb_qc` 一次挂多项时，要挑一个**有明确路由**的 cause（而不是被没有路由的那个卡住）。"""
    r = KernelResult(
        tool="kb_qc", ok=False, cause=None, exit_code=2,
        report={"ok": False, "causes": ["从未登记过的_cause", "srt_bom"]},
    )
    d = decide(S.RUN_QC, r)
    assert d.goto == S.BUILD_SRT and d.cause == "srt_bom"


# ---------------------------------------------------------------- 预算护栏
def test_same_cause_twice_stops():
    st = state(cause_history=["srt_bom"])           # 已经失败过一次
    d = decide(S.RUN_QC, fail("srt_bom", node_causes=["srt_bom"]), st)
    assert d.halt is True
    assert "连续" in d.reason


def test_same_cause_recovers_after_a_success():
    """中间成功过一次 ⇒ 连续计数归零，不该被历史计数误判成"老问题没解决"。"""
    st = state(cause_history=["srt_bom", "cut_duration_mismatch"])
    assert consecutive_same_cause(st, "srt_bom") == 0
    d = decide(S.RUN_QC, fail("srt_bom", node_causes=["srt_bom"]), st)
    assert not d.halt and d.goto == S.BUILD_SRT


def test_retake_budget_exhausts():
    st = state(retakes={S.PLAN_CUT: CFG.budgets.retake_budget})
    d = decide(S.RUN_QC, fail("junction_glitch", node_causes=["junction_glitch"]), st)
    assert d.halt is True
    assert "上限" in d.reason


def test_retake_count_is_per_target_stage():
    """预算按**目标阶段**计：别的阶段回退过，不该影响这个阶段。"""
    st = state(retakes={S.BUILD_SRT: 99})
    d = decide(S.RUN_QC, fail("junction_glitch", node_causes=["junction_glitch"]), st)
    assert not d.halt and d.goto == S.PLAN_CUT


def test_cross_agent_loop_budget_is_one():
    st = state(cross_agent_events=[{"node": "x"}])
    d = decide(S.BUILD_SRT, fail("cross_deleted_sentence", node_causes=["cross_deleted_sentence"]), st)
    assert d.halt is True
    assert "跨 agent" in d.reason

    # 第一次允许，且被标记为跨 agent（要记账）
    d1 = decide(S.BUILD_SRT, fail("cross_deleted_sentence",
                                  node_causes=["cross_deleted_sentence"]))
    assert not d1.halt and d1.cross_agent is True and d1.goto == S.PLAN_CUT


def test_expensive_route_requires_user_approval():
    d = decide(S.PLAN_FINAL, fail("bgm_price_above_budget",
                                  node_causes=["bgm_price_above_budget"]))
    # 目标是**审批节点**而不是 HALT：用户可能同意，那就得继续回退
    assert d.goto == S.APPROVAL
    assert d.needs_approval is True
    assert d.halt is False
    assert d.approval_key == "expensive:bgm_price_above_budget"
    assert d.resume_target == S.PLAN_FINAL, "同意之后要回到哪，必须随裁决一起带出去"

    # 用户已授权 ⇒ 正常回退
    st = state(goal={**initial_state("x")["goal"], "allow_expensive": True})
    d2 = decide(S.PLAN_FINAL, fail("bgm_price_above_budget",
                                   node_causes=["bgm_price_above_budget"]), st)
    assert d2.halt is False and d2.goto == S.PLAN_FINAL


def test_advance_records_pending_approval():
    goto, update = R.advance(
        node=S.PLAN_FINAL,
        result=fail("bgm_price_above_budget", node_causes=["bgm_price_above_budget"]),
        state=state(), config=CFG)
    assert goto == S.APPROVAL
    p = update["pending_approval"]
    assert p["resume_target"] == S.PLAN_FINAL
    assert p["approval_key"].startswith("expensive:")
    assert "halts" not in update, "请人拍板不等于停下 —— 它是流程中的一步"


def test_graph_target_maps_human_to_halt():
    assert R.graph_target(R.HUMAN) == S.HALT
    assert R.graph_target(S.BUILD_SRT) == S.BUILD_SRT


# ------------------------------------------------------- 评审指定回退（独立入口）
def test_decide_manual_validates_target():
    d = R.decide_manual(node=S.REVIEW_FINAL, target="no_such_node",
                        cause="reviewer_rejected", reason="重做", state=state(), config=CFG)
    assert d.halt is True
    d2 = R.decide_manual(node=S.REVIEW_FINAL, target=S.HALT,
                         cause="reviewer_rejected", reason="重做", state=state(), config=CFG)
    assert d2.halt is True, "评审不许把控制节点当回退目标"


def test_decide_manual_shares_the_same_guards():
    """⛔ 独立入口不能变成绕过预算的后门 —— 三条护栏必须同样生效。"""
    st = state(retakes={S.BUILD_SRT: CFG.budgets.retake_budget})
    d = R.decide_manual(node=S.REVIEW_TEST, target=S.BUILD_SRT,
                        cause="reviewer_rejected", reason="字幕不好", state=st, config=CFG)
    assert d.halt is True

    st2 = state(cause_history=["reviewer_rejected"])
    d2 = R.decide_manual(node=S.REVIEW_TEST, target=S.BUILD_SRT,
                         cause="reviewer_rejected", reason="字幕不好", state=st2, config=CFG)
    assert d2.halt is True, "同因连续 2 次也必须停"


def test_decide_manual_self_target_is_resume():
    d = R.decide_manual(node=S.REVIEW_TEST, target=S.REVIEW_TEST,
                        cause="reviewer_rejected", reason="再想一次",
                        state=state(), config=CFG)
    assert d.kind == R.KIND_RESUME and d.goto == S.REVIEW_TEST
    assert d.consume_retake is False


def test_budgets_are_configurable():
    cfg = AppConfig(budgets=Budgets(retake_budget=0))
    d = decide(S.RUN_QC, fail("junction_glitch", node_causes=["junction_glitch"]), cfg=cfg)
    assert d.halt is True


# ---------------------------------------------------------------- advance 记账
def test_advance_records_history_and_budget():
    st = state()
    goto, update = R.advance(node=S.RUN_QC, result=fail("srt_bom", node_causes=["srt_bom"]),
                             state=st, config=CFG)
    assert goto == S.BUILD_SRT
    assert update["cause_history"] == ["srt_bom"]
    assert update["retakes"] == {S.BUILD_SRT: 1}
    assert update["route_history"][0]["goto"] == S.BUILD_SRT
    assert S.RUN_QC in update["results"]


def test_advance_halt_records_halt_event():
    goto, update = R.advance(node=S.RUN_QC, result=fail("missing_stream"),
                             state=state(), config=CFG)
    assert goto == R.HUMAN
    assert update["halts"][0]["cause"] == "missing_stream"


def test_advance_on_success_adds_no_cause():
    goto, update = R.advance(node=S.BUILD_SRT, result=ok(), state=state(), config=CFG)
    assert goto == S.BURN_TEST
    assert "cause_history" not in update
    assert "retakes" not in update


def test_route_history_render_lists_only_failures():
    st = state(route_history=[
        {"node": "build_srt", "ok": True, "causes": [], "goto": "burn_test",
         "kind": "ok", "cost": "free", "reason": "通过"},
        {"node": "run_qc", "ok": False, "causes": ["srt_bom"], "goto": "build_srt",
         "kind": "fix", "cost": "low", "reason": "重跑 srt"},
    ])
    lines = R.render_route_history(st)
    assert len(lines) == 1 and "srt_bom" in lines[0]
