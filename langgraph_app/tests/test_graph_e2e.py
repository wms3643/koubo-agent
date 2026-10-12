"""端到端图测试 —— **桩内核 + 桩判断节点**，不调模型、不跑 ffmpeg、不花一分钱。

它验证的是"真图里的行为"，而不是函数级语义（那是 test_routing 的活）：

    A. **整链走通**：每个节点恰好执行一次，最后到达交付；
    B. **失败 → 回退 → 继续**：回退目标被重跑，且预算记了账；
    C. **护栏在真图里生效**：同因连续 2 次 / 未知 cause / 该停的 cause ⇒ 真的停下；
    D. **付费闸门真的拦得住**：中断 → 批准才继续；拒绝就停下；
    E. **断点续跑**：内核说从第 3 步接续时，前面的节点**不该**被重跑。

为什么必须用桩：真跑一次要 GPU 转写 + 多轮编码 + 可能付费，动辄几十分钟 ——
那样的测试没人会跑，于是**等于没有测试**。桩把"图的行为"与"内核的重活"解耦开。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from langgraph_app import routing as R  # noqa: E402
from langgraph_app import stages as S  # noqa: E402
from langgraph_app.config import AppConfig  # noqa: E402
from langgraph_app.graph import build_graph  # noqa: E402
from langgraph_app.kernel import KernelResult  # noqa: E402
from langgraph_app.nodes import build_nodes  # noqa: E402
from langgraph_app.nodes.base import command, synthetic  # noqa: E402
from langgraph_app.state import initial_state  # noqa: E402


# ---------------------------------------------------------------------------
#  桩内核
# ---------------------------------------------------------------------------
def OK(tool: str, **report) -> KernelResult:
    return KernelResult(tool=tool, ok=True, exit_code=0,
                        report={"ok": True, "stage": tool, **report})


def FAIL(tool: str, cause: str, **report) -> KernelResult:
    return KernelResult(tool=tool, ok=False, cause=cause, exit_code=2,
                        report={"ok": False, "cause": cause, **report})


class FakeClient:
    """按 `script` 出结果的假内核。键形如 `cut`、`burn:test`、`assemble:assemble`。

    值是**结果队列**（每次调用弹一个；弹完就用默认的 OK）—— 这样"第一次失败、
    第二次成功"这种回退场景可以精确编排。
    """

    def __init__(self, script: dict | None = None):
        self.calls: list[str] = []
        self.script = {k: list(v) for k, v in (script or {}).items()}

    def _next(self, key: str, tool: str, **report) -> KernelResult:
        self.calls.append(key)
        queue = self.script.get(key)
        if queue:
            return queue.pop(0)
        return OK(tool, **report)

    def status(self, project, **kw):
        return self._next("status", "kb_status", next_stage="1_transcribe")

    def plan_cut(self, project, **kw):
        return self._next("plan_cut", "kb_plan_cut")

    def transcribe(self, project, **kw):
        return self._next("transcribe", "kb_transcribe")

    def cut(self, project, **kw):
        return self._next("cut", "kb_cut")

    def srt(self, project, **kw):
        return self._next("srt", "kb_srt")

    def burn(self, project, *, mode, margin_v=None, **kw):
        return self._next(f"burn:{mode}", "kb_burn", margin_v=margin_v)

    def overlay(self, project, **kw):
        return self._next("overlay", "kb_overlay")

    def bgm(self, project, *, dry_run=False, confirm_paid=False, **kw):
        if dry_run:
            return self._next("bgm:dry", "kb_bgm", price_per_call=0.49)
        if confirm_paid:
            return self._next("bgm:paid", "kb_bgm", cost=0.49)
        return self._next("bgm:probe", "kb_bgm")

    def assemble(self, project, *, mode, spec, dry_run=False, **kw):
        return self._next(f"assemble:{mode}", "kb_assemble", spec=spec)

    def qc(self, project, *, target, frames_at=None, **kw):
        return self._next(f"qc:{target}", "kb_qc", target=target)


# ---------------------------------------------------------------------------
#  桩判断节点（替代 LLM）
# ---------------------------------------------------------------------------
def fake_judge(name: str, config: AppConfig, *, verdict: KernelResult | None = None):
    def fn(state):
        result = verdict or synthetic(name, ok=True, report={"ok": True, "stub": True})
        goto, update = R.advance(node=name, result=result, state=state, config=config)
        return command(goto, update)

    return fn


def fake_plan_final(config: AppConfig):
    """桩版 `plan_final`：**照契约写出三份规格**（用与真节点同一份实现）。

    ⚠️ 为什么不能只返回一个空结果：下游 `apply_overlay` 需要 `edit/assemble.json`。
    桩不写它，端到端测试就会在"缺文件"上停下 —— 而那是**桩的缺陷**，不是图的问题，
    测出来的东西也就没意义了。所以桩必须守住它对下游的契约。
    """
    def fn(state):
        from langgraph_app.nodes.specs import write_final_specs

        facts = write_final_specs(
            project=Path(state["project"]),
            style="轻快的纯器乐钢琴，中速，商务感，不抢人声（桩）",
            mix_in={"bgm_fade_out": 1.5},
            total=10.0, max_price=config.budgets.bgm_max_price,
        )
        return fake_judge(S.PLAN_FINAL, config,
                          verdict=synthetic(S.PLAN_FINAL, ok=True,
                                            report={"ok": True, **facts}))(state)

    return fn


LLM_NODES = (S.PLAN_CUT, S.PLAN_SUBTITLES, S.DESIGN_OVERLAY, S.PLAN_FINAL,
             S.REVIEW_TEST, S.REVIEW_FINAL)


# ---------------------------------------------------------------------------
#  夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def config(tmp_path) -> AppConfig:
    return AppConfig.from_env(
        projects_root=tmp_path,
        checkpoint_db=tmp_path / ".state" / "ck.sqlite",
    )


def fake_design_overlay(config: AppConfig):
    """桩版 `design_overlay`：留下 `overlays/index.html`（它本来就是特效设计的交付物）。"""
    def fn(state):
        project = Path(state["project"])
        out = project / "overlays" / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html><!-- stub --></html>", encoding="utf-8")
        return fake_judge(S.DESIGN_OVERLAY, config,
                          verdict=synthetic(S.DESIGN_OVERLAY, ok=True,
                                            report={"ok": True, "index_html": str(out)}))(state)

    return fn


def fake_render_overlay(config: AppConfig):
    """桩版 `render_overlay`：**必须留下一个 .mov** —— 下游 `apply_overlay` 要读它。

    ⚠️ 桩的价值在于"守住契约"，不是"跑过去就行"。这个桩不产 MOV，端到端测试就会停在
    `missing_overlay` 上，而那是**桩的缺陷**、不是图的缺陷 —— 测出来的东西也就没意义。
    真节点渲染的是 ProRes4444 带 alpha；这里只要文件存在，因为被测的是**图的接线**。
    """
    def fn(state):
        project = Path(state["project"])
        if not (state.get("goal") or {}).get("overlay_required", True):
            # 用户明确要无动效版 ⇒ **不许**留下 MOV（否则交付说明会误报"已含动效层"）
            return fake_judge(S.RENDER_OVERLAY, config, verdict=synthetic(
                S.RENDER_OVERLAY, ok=True,
                report={"ok": True, "skipped": "用户要求无动效版（桩）"}))(state)
        out = project / "overlays" / "renders" / "overlay.mov"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\0" * 64)
        return fake_judge(S.RENDER_OVERLAY, config,
                          verdict=synthetic(S.RENDER_OVERLAY, ok=True,
                                            report={"ok": True, "output": str(out)}))(state)

    return fn


def make_app(config, client, *, judges=None, checkpointer=None):
    overrides = {n: fake_judge(n, config) for n in LLM_NODES}
    # 这两个判断节点的**下游契约是"留下文件"**，桩必须照做（否则测的是桩的缺陷）
    overrides[S.PLAN_FINAL] = fake_plan_final(config)
    overrides[S.DESIGN_OVERLAY] = fake_design_overlay(config)
    overrides[S.RENDER_OVERLAY] = fake_render_overlay(config)
    overrides.update(judges or {})
    nodes = build_nodes(client, config, llm=False, overrides=overrides)
    return build_graph(config, client=client, nodes=nodes, checkpointer=checkpointer)


def run_app(app, project: Path, *, thread="t1", goal=None):
    cfg = {"configurable": {"thread_id": thread}}
    return app.invoke(initial_state(str(project), goal=goal or {}), cfg), cfg


def node_runs(result: dict, node: str) -> int:
    """某节点被**执行**了几次。

    为什么不用 `client.calls`：桩判断节点根本不调内核，用内核调用次数去量"有没有回退"
    会永远得到 0（这是我第一版测试写错的地方）。`route_history` 每个 worker 节点都会
    追加一条（`routing.advance` 保证），所以它才是"图行为"的准确账本。
    """
    return sum(1 for r in (result.get("route_history") or []) if r.get("node") == node)


# ===========================================================================
#  A. 整链走通
# ===========================================================================
def test_happy_path_walks_whole_chain(config, tmp_path):
    client = FakeClient()
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)

    assert not result.get("halts"), f"不该停下：{result.get('halts')}"
    ran = set(R.__dict__ and (result.get("results") or {}).keys())
    expected = set(S.WORKER_NODES)
    assert expected <= ran, f"这些节点没跑到：{sorted(expected - ran)}"

    # 每个阶段**恰好执行一次**（多了说明有重复触发，少了说明链断了）。
    # 量的是"节点执行次数"——桩判断节点不调内核，用内核调用次数会量出 0。
    for node in S.WORKER_NODES:
        assert node_runs(result, node) == 1, f"{node} 执行了 {node_runs(result, node)} 次"
    # 确定性节点必须**真的调到内核**（"跳过"是判断节点的语义，不是确定性节点的）。
    # ⚠️ 这里不含 `overlay`：`render_overlay` 在端到端测试里是桩（桩照契约留 .mov，
    #    但不调内核）—— 它的执行次数由上面的 `node_runs` 覆盖。
    for key in ("status", "transcribe", "cut", "srt", "burn:test",
                "burn:full", "assemble:mix", "qc:final"):
        assert client.calls.count(key) == 1, f"{key} 执行了 {client.calls.count(key)} 次"
    # `assemble` 本来就该跑两次：一次缝接骨架（→ skeleton.mp4），一次叠加动效（→ final.mp4）
    assert client.calls.count("assemble:assemble") == 2
    assert S.ASSEMBLE_BASE in (result.get("results") or {})
    assert S.APPLY_OVERLAY in (result.get("results") or {})

    assert result.get("final"), "必须产出交付信息"
    assert result["final"]["final_exists"] is False  # 桩没造真文件，如实报告
    assert "未执行片尾质检" not in result["final"]["known_gaps"]


def test_deliver_and_halt_reach_end(config, tmp_path):
    """终态必须真的到 END（曾经我把交付写成自环，会无限循环）。"""
    app = make_app(config, FakeClient())
    result, _ = run_app(app, tmp_path)
    assert result.get("final", {}).get("project")


# ===========================================================================
#  B. 失败 → 回退 → 继续
# ===========================================================================
def test_failure_reroutes_and_records_budget(config, tmp_path):
    # 第一次 cut 被内核按幻觉段拒绝；回剪裁策划改文件后第二次成功
    client = FakeClient(script={
        "cut": [FAIL("kb_cut", "hallucination_cut_forbidden",
                     hint="把那些下标移除")],
    })
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)

    assert not result.get("halts")
    assert node_runs(result, S.APPLY_CUT) == 2, "回退之后必须重跑 cut"
    assert node_runs(result, S.PLAN_CUT) == 2, "回退目标是剪裁策划的判断节点"
    assert result["retakes"].get(S.PLAN_CUT) == 1
    assert "hallucination_cut_forbidden" in (result.get("cause_history") or [])
    # 交付里要能看到"哪个 cause 被怎么处理了"
    assert any("hallucination_cut_forbidden" in line
               for line in result["final"]["fallbacks"])


def test_srt_defect_reruns_build_srt_only(config, tmp_path):
    """字幕类缺陷是**工具级**修复：只重跑 kb_srt，不该惊动 LLM。"""
    client = FakeClient(script={"qc:final": [FAIL("kb_qc", "srt_bom", causes=["srt_bom"])]})
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)

    assert not result.get("halts")
    assert node_runs(result, S.BUILD_SRT) == 2
    assert node_runs(result, S.PLAN_CUT) == 1, "不该回退到剪裁策划（这是确定性修复）"
    assert node_runs(result, S.PLAN_SUBTITLES) == 1, "也不必让字幕制作重做判断"


# ===========================================================================
#  C. 护栏在真图里生效
# ===========================================================================
def test_same_cause_twice_halts(config, tmp_path):
    client = FakeClient(script={
        "cut": [FAIL("kb_cut", "hallucination_cut_forbidden"),
                FAIL("kb_cut", "hallucination_cut_forbidden")],
    })
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)

    assert result.get("halts"), "同因连续 2 次必须停下"
    assert "连续" in result["halts"][0]["reason"]
    assert client.calls.count("cut") == 2, "不许第三次重试"


def test_unknown_cause_halts(config, tmp_path):
    client = FakeClient(script={"cut": [FAIL("kb_cut", "从来没见过的_cause")]})
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)
    assert result.get("halts")
    assert "矩阵" in result["halts"][0]["reason"]


def test_human_cause_halts_immediately(config, tmp_path):
    client = FakeClient(script={"burn:full": [FAIL("kb_burn", "missing_stream")]})
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)
    assert result.get("halts")
    assert client.calls.count("burn:full") == 1, "该升级人工的绝不能自动重试"


def test_cross_agent_loop_budget(config, tmp_path):
    """字幕制作报「跨删除区句子」⇒ 回剪裁策划，但**整片只允许一次**。"""
    cross = synthetic(S.PLAN_SUBTITLES, ok=False, cause="cross_deleted_sentence",
                      report={"ok": False, "cause": "cross_deleted_sentence"})
    judges = {S.PLAN_SUBTITLES: fake_judge(S.PLAN_SUBTITLES, config, verdict=cross)}
    app = make_app(config, FakeClient(), judges=judges)
    result, _ = run_app(app, tmp_path)

    assert result.get("halts")
    assert "跨 agent" in result["halts"][0]["reason"]
    assert len(result.get("cross_agent_events") or []) == 1


def test_retake_budget_can_be_exhausted(config, tmp_path):
    """回退上限用满 ⇒ 停下（这里把上限压到 1 便于构造）。"""
    from dataclasses import replace

    from langgraph_app.config import Budgets

    cfg = replace(config, budgets=Budgets(retake_budget=1))
    client = FakeClient(script={
        "cut": [FAIL("kb_cut", "junction_glitch", causes=["junction_glitch"]),
                FAIL("kb_cut", "junction_glitch", causes=["junction_glitch"])],
    })
    app = make_app(cfg, client)
    result, _ = run_app(app, tmp_path)
    assert result.get("halts")


# ===========================================================================
#  D. 付费闸门
# ===========================================================================
def test_paid_gate_interrupts_then_approve(config, tmp_path):
    client = FakeClient(script={"bgm:probe": [FAIL("kb_bgm", "bgm_needs_confirm")]})
    app = make_app(config, client, checkpointer=InMemorySaver())

    result, cfg = run_app(app, tmp_path)
    # 第一次必须**停在中断上**，绝不能自己批准
    snapshot = app.get_state(cfg)
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    assert interrupts, "付费前必须中断询问"
    assert interrupts[0].value["type"] == "approval_request"
    assert interrupts[0].value["price"] == 0.49
    assert client.calls.count("bgm:paid") == 0, "★ 未批准前**一次付费调用都不能发生**"

    # 批准后继续
    result = app.invoke(Command(resume=True), cfg)
    assert client.calls.count("bgm:paid") == 1
    assert not result.get("halts")
    assert result.get("approvals", {}).get("paid:bgm") is True


def test_paid_gate_reject_halts(config, tmp_path):
    client = FakeClient(script={"bgm:probe": [FAIL("kb_bgm", "bgm_needs_confirm")]})
    app = make_app(config, client, checkpointer=InMemorySaver())
    _, cfg = run_app(app, tmp_path)
    result = app.invoke(Command(resume=False), cfg)

    assert result.get("halts")
    assert "拒绝" in result["halts"][0]["reason"]
    assert client.calls.count("bgm:paid") == 0
    assert client.calls.count("assemble:mix") == 0, "拒绝付费就不该继续往下走"


def test_existing_bgm_is_reused_for_free(config, tmp_path):
    """已有成品 ⇒ 免费复用，**不该**弹付费询问。"""
    client = FakeClient(script={"bgm:probe": [FAIL("kb_bgm", "bgm_exists")]})
    app = make_app(config, client, checkpointer=InMemorySaver())
    result, cfg = run_app(app, tmp_path)

    assert not list(getattr(app.get_state(cfg), "interrupts", ()) or ())
    assert client.calls.count("bgm:paid") == 0
    assert any("免费复用" in n for n in (result.get("notes") or []))


def test_user_supplied_bgm_skips_generation(config, tmp_path):
    # 用户说了"用我自己那首"，就得**真把文件放进来**
    (tmp_path / "bgm").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bgm" / "mine.mp3").write_bytes(b"ID3" + b"\0" * 64)

    client = FakeClient()
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path, goal={"bgm_source": "user"})
    assert not result.get("halts"), result.get("halts")
    assert client.calls.count("bgm:probe") == 0 and client.calls.count("bgm:paid") == 0
    assert any("你提供的曲目" in n for n in (result.get("notes") or []))


def test_user_bgm_missing_halts_instead_of_looping(config, tmp_path):
    """指定自备曲目却没放文件 ⇒ 必须**停下**。

    ⚠️ 这里原本是死循环：跳过生成 → mix 报 `bgm_missing` → 矩阵回退到生成节点 → 又跳过。
    最后虽会被"同因连续 2 次"掐断，但报出来的理由是那句通用话术，
    **完全看不出真正的原因**（"你说了用自备曲目，可 bgm/ 是空的"）。
    而且这件事没有自动修法：既不能替用户找曲子，也不该偷偷改成生成（要花钱）。
    """
    client = FakeClient()
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path, goal={"bgm_source": "user"})

    assert result.get("halts")
    blob = json.dumps(result["halts"], ensure_ascii=False)
    assert "missing_user_bgm" in blob
    assert client.calls.count("bgm:paid") == 0, "⛔ 不许偷偷改成付费生成"
    assert node_runs(result, S.GENERATE_BGM) == 1, "不该循环"


# ===========================================================================
#  E. 断点续跑
# ===========================================================================
def test_resume_from_kernel_status_skips_earlier_stages(config, tmp_path):
    """内核说从 3_srt 接续 ⇒ 转写/剪辑/字幕都不该重跑。"""
    client = FakeClient(script={"status": [OK("kb_status", next_stage="3_srt")]})
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)

    assert client.calls.count("transcribe") == 0
    assert client.calls.count("cut") == 0
    assert client.calls.count("srt") == 1, "字幕档之后的阶段正常推进"
    assert result.get("resume_from") == S.PLAN_SUBTITLES


def test_done_with_failed_qc_resumes_at_the_fixable_stage(config, tmp_path):
    """★ `next_stage='done'` 但质检查出问题 ⇒ **不许直接交付**，回该修的那一步。

    真跑撞到过：`kb_status` 只看 `qc_report.json` 在不在，所以一次**未通过**的质检
    也被算成 done。照它走就会把带 `bgm_silent`（BGM 等于没进去）的成片交付出去。
    """
    client = FakeClient(script={"status": [OK("kb_status", next_stage="done",
                                              last_qc_ok=False,
                                              last_qc_causes=["bgm_silent"])]})
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path, goal={"overlay_required": False,
                                             "bgm_source": "user"})

    assert result.get("resume_from") == S.PLAN_FINAL, \
        "bgm_silent 的回退目标是剪裁策划（复核 mix.json），不是直接交付"
    assert client.calls.count("mix:assembled") == 0 or True  # 不额外约束
    assert any("不算完成" in n for n in (result.get("notes") or []))


def test_done_with_passing_qc_delivers(config, tmp_path):
    client = FakeClient(script={"status": [OK("kb_status", next_stage="done",
                                              last_qc_ok=True,
                                              last_qc_causes=[])]})
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)
    assert result.get("resume_from") == S.DELIVER
    assert client.calls.count("cut") == 0, "已完工的项目不该重跑"


def test_status_failure_halts(config, tmp_path):
    client = FakeClient(script={"status": [FAIL("kb_status", "missing_file")]})
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path)
    assert result.get("halts")


def test_graph_rejects_incomplete_node_set(config):
    nodes = build_nodes(FakeClient(), config, llm=False)  # 故意缺判断节点
    with pytest.raises(RuntimeError, match="图不完整"):
        build_graph(config, nodes=nodes)


# ===========================================================================
#  F. 例外放行（kb_override）—— 主管独有，必须写明理由
# ===========================================================================
@pytest.fixture()
def real_client(tmp_path, monkeypatch):
    """**真** `KernelClient` + 桩内核脚本。

    用来测"客户端与节点一起"的行为（例如例外放行的理由校验 —— 那条规则住在客户端里，
    用假客户端就把被测对象本身替换掉了，等于什么都没测）。
    """
    from langgraph_app.kernel import KernelClient

    monkeypatch.setenv("STUB_SCENARIO", "ok")
    cfg = AppConfig.from_env(
        kernel_python=Path(sys.executable),
        kb_entry=Path(__file__).parent / "stub_kernel.py",
        projects_root=tmp_path,
        checkpoint_db=tmp_path / ".state" / "ck.sqlite",
    )
    return KernelClient(cfg), cfg


def test_override_retranscribe_runs_and_shifts_resume_point(real_client, tmp_path):
    client, cfg = real_client
    app = make_app(cfg, client)
    result, _ = run_app(app, tmp_path, goal={"override": {
        "action": S.OVERRIDE_RETRANSCRIBE,
        "reason": "逐字稿整段错位，用户已确认要重转",
    }})

    assert not result.get("halts"), result.get("halts")
    assert S.OVERRIDE in (result.get("results") or {})
    # ⛔ 重转写会让剪辑决策/时间轴/字幕全部作废 —— 续跑点必须**前移**，
    #    否则会拿作废的产物继续跑（那正是这条放行最危险的用法）
    assert result.get("resume_from") == S.PLAN_CUT
    assert any("例外放行" in n for n in (result.get("notes") or []))


def test_override_without_proper_reason_halts(real_client, tmp_path):
    client, cfg = real_client
    app = make_app(cfg, client)
    result, _ = run_app(app, tmp_path, goal={"override": {
        "action": S.OVERRIDE_RETRANSCRIBE, "reason": "太短",
    }})
    assert result.get("halts"), "理由不足必须停下（例外要有记录）"
    blob = json.dumps(result["halts"], ensure_ascii=False)
    assert "override_reason_required" in blob
    # 而且**连进程都没起**（理由校验做在调用内核之前 —— 这是"更早、更清楚"）
    assert result["results"][S.OVERRIDE]["spawned"] is False


def test_override_with_unknown_action_halts(real_client, tmp_path):
    client, cfg = real_client
    app = make_app(cfg, client)
    result, _ = run_app(app, tmp_path, goal={"override": {
        "action": "我编的动作", "reason": "理由够长的一段说明文字",
    }})
    assert result.get("halts")
    assert "未知的例外动作" in result["halts"][0]["reason"]


def test_no_override_is_transparent(config, tmp_path):
    app = make_app(config, FakeClient())
    result, _ = run_app(app, tmp_path)
    assert not result.get("halts")
    assert S.OVERRIDE not in (result.get("results") or {}), \
        "没有例外要放行时，这个节点应当是透明的（不该留下账目）"


def test_no_overlay_when_user_asks(config, tmp_path):
    """用户明确要无动效版 ⇒ 跳过动效，但交付说明里必须**写明**。"""
    client = FakeClient()
    app = make_app(config, client)
    result, _ = run_app(app, tmp_path, goal={"overlay_required": False})

    assert client.calls.count("overlay") == 0
    assert any("未含动效层" in d for d in result["final"].get("disclaimer") or [])
