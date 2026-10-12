"""演练模式 —— **桩内核 + 桩判断节点**，跑同一张图，不花钱、不跑 ffmpeg、不需要密钥。

它解决的是一个很具体的问题：真实跑一遍要 GPU 转写 + 多轮编码 + 可能付费，
动辄几十分钟；而"这套编排好不好用、闸门拦不拦得住、回退怎么走"这些问题，
需要在几十秒内就能回答。端到端测试（`tests/test_graph_e2e.py`）早就证明了桩的价值，
本模块只是把那套桩**接进界面**，让人能亲眼看着它跑。

⭐ **跑的是同一张图、同一份路由矩阵、同一套护栏**。被换掉的只有两类东西：
    ① 内核（真 ffmpeg/GPU 的重活 → 按剧本返回结果）
    ② 判断节点（真模型 → 固定裁决）
   节点本身（`nodes/kernel_nodes.py`、`nodes/control.py`）**一行没改** —— 所以界面里
   看到的回退、记账、闸门，就是真跑会发生的行为。

⛔ **边界（必须对用户说清）**：演练**不产生真实产物**。写盘的只有桩自己造的占位文件
   （`overlays/index.html`、`overlays/renders/*.mov`、三份规格 JSON），
   落在 `projects/_rehearsal/<场景>_<nonce>/`，与真实项目目录完全隔开。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import routing as R
from .. import stages as S
from ..config import AppConfig
from ..graph import build_graph
from ..kernel import KernelResult
from ..nodes import build_nodes
from ..nodes.base import NodeFn, command, synthetic
from ..nodes.specs import write_final_specs

# 演练项目的落地目录（相对 projects_root）——单独一层，避免和真实项目混在一起
REHEARSAL_DIR = "_rehearsal"


# ============================================================================
#  桩内核
# ============================================================================
def _OK(tool: str, **report) -> KernelResult:
    return KernelResult(tool=tool, ok=True, exit_code=0,
                        report={"ok": True, "stage": tool, **report})


def _FAIL(tool: str, cause: str, **report) -> KernelResult:
    return KernelResult(
        tool=tool, ok=False, cause=cause, exit_code=2,
        report={"ok": False, "stage": tool, "cause": cause, **report},
    )


class StubKernel:
    """按剧本出结果的假内核。

    `script` 的键形如 `cut` / `burn:test` / `assemble:mix` / `bgm:probe`，
    值是**结果队列**：每次调用弹一个，弹完就返回默认的 OK。
    这样"第一次失败、第二次成功"这种回退场景可以精确编排 —— 与真内核的调用签名
    一一对应（方法名和关键字参数照抄 `KernelClient`），所以节点代码完全不知道自己在跟桩说话。
    """

    def __init__(self, script: dict[str, list[KernelResult]] | None = None):
        self.calls: list[str] = []
        # ⚠️ 逐层复制：`pop` 会改列表，共用一份会让第二次运行看到"队列已空"
        self.script = {k: list(v) for k, v in (script or {}).items()}

    def _next(self, key: str, tool: str, **report) -> KernelResult:
        self.calls.append(key)
        queue = self.script.get(key)
        if queue:
            return queue.pop(0)
        return _OK(tool, **report)

    # ---- 以下签名与 `KernelClient` 保持一致（节点是按这个契约调用的）--------
    def status(self, project, **kw):
        return self._next("status", "kb_status", next_stage="1_transcribe")

    def transcribe(self, project, **kw):
        return self._next("transcribe", "kb_transcribe", segment_count=23)

    def plan_cut(self, project, **kw):
        return self._next("plan_cut", "kb_plan_cut")

    def cut(self, project, *, run=False, **kw):
        return self._next("cut", "kb_cut", run=run)

    def srt(self, project, **kw):
        return self._next("srt", "kb_srt")

    def burn(self, project, *, mode, margin_v=None, **kw):
        return self._next(f"burn:{mode}", "kb_burn", mode=mode, margin_v=margin_v)

    def overlay(self, project, *, output=None, **kw):
        return self._next("overlay", "kb_overlay", output=output)

    def bgm(self, project, *, dry_run=False, confirm_paid=False, **kw):
        if dry_run:
            return self._next("bgm:dry", "kb_bgm", price_per_call=0.49)
        if confirm_paid:
            return self._next("bgm:paid", "kb_bgm", cost=0.49)
        return self._next("bgm:probe", "kb_bgm")

    def assemble(self, project, *, mode, spec, dry_run=False, **kw):
        return self._next(f"assemble:{mode}", "kb_assemble", mode=mode, spec=spec)

    def qc(self, project, *, target, frames_at=None, **kw):
        return self._next(f"qc:{target}", "kb_qc", target=target)

    # 例外放行（只有 `--override` 那条路会走到；演练里给它一个成功结果即可）
    def override_retranscribe(self, project, *, reason="", **kw):
        return self._next("override:retranscribe", "kb_override", action="retranscribe")

    def override_allow_hallucination_cut(self, project, *, reason="", **kw):
        return self._next("override:allow_hallucination_cut", "kb_override",
                          action="allow_hallucination_cut")


# ============================================================================
#  桩判断节点（替代 LLM）
# ============================================================================
def _judge(name: str, config: AppConfig) -> NodeFn:
    """固定裁决：通过。记账仍然走 `R.advance()`（唯一那一份实现）。"""
    def fn(state):
        result = synthetic(name, ok=True,
                           report={"ok": True, "stub": "演练：判断节点已用桩替身"})
        goto, update = R.advance(node=name, result=result, state=state, config=config)
        return command(goto, update)

    return fn


def _plan_cut(config: AppConfig) -> NodeFn:
    """桩版剪裁策划的判断。

    ⚠️ 它**不读** `edit/plan_context.json`（真节点要读）—— 但**必须**在回退到它时
    表现得和真节点一样"重新做一次判断"，否则第二条 `cut` 永远等不到重试。
    这里靠 `R.advance` 保证：回退会真的把控制权交回本节点，本节点再放行。
    """
    return _judge(S.PLAN_CUT, config)


def _plan_final(config: AppConfig) -> NodeFn:
    """桩版 `plan_final`：**照契约写出三份规格**（用与真节点同一份实现）。

    ⚠️ 为什么不能只返回一个空结果：下游 `apply_overlay` 要读 `edit/assemble.json`。
    桩不写它，演练就会停在"缺文件"上 —— 而那是**桩的缺陷**，不是编排的问题，
    演出来的东西也就没有参考价值。桩必须守住它对下游的契约。
    """
    def fn(state):
        facts = write_final_specs(
            project=Path(state["project"]),
            style="轻快的纯器乐钢琴，中速，商务感，不抢人声（演练桩）",
            mix_in={"bgm_fade_out": 1.5},
            total=60.0, max_price=config.budgets.bgm_max_price,
        )
        return _finish(S.PLAN_FINAL, config, {"ok": True, **facts})(state)

    return fn


def _finish(name: str, config: AppConfig, report: dict) -> NodeFn:
    def fn(state):
        goto, update = R.advance(node=name, result=synthetic(name, ok=True, report=report),
                                 state=state, config=config)
        return command(goto, update)

    return fn


def _design_overlay(config: AppConfig) -> NodeFn:
    """桩版动效设计：留下 `overlays/index.html`（它本来就是这一步的交付物）。"""
    def fn(state):
        out = Path(state["project"]) / "overlays" / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "<!doctype html><meta charset=utf-8>"
            "<body style='background:#111;color:#eee;font-family:sans-serif'>"
            "<h1>演练占位：动效工程（真跑时这里由 HyperFrames 渲染）</h1></body>",
            encoding="utf-8")
        return _finish(S.DESIGN_OVERLAY, config,
                       {"ok": True, "index_html": str(out), "stub": True})(state)

    return fn


def _render_overlay(config: AppConfig) -> NodeFn:
    """桩版动效渲染：**必须留下一个 `.mov`** —— 下游 `apply_overlay` 要读它。

    ⚠️ 桩的价值在于"守住契约"，不是"跑过去就行"。不产 MOV，演练就会停在
    `missing_overlay` 上，而那暴露的是桩的缺陷，不是编排的缺陷。
    真节点渲染的是 ProRes4444 带 alpha；这里只要文件在，因为被观察的是**接线与闸门**。
    """
    def fn(state):
        project = Path(state["project"])
        if not (state.get("goal") or {}).get("overlay_required", True):
            # 用户明确要无动效版 ⇒ 不许留 MOV（否则交付说明会误报"已含动效层"）
            return _finish(S.RENDER_OVERLAY, config,
                           {"ok": True, "skipped": "用户要求无动效版（演练桩）"})(state)
        out = project / "overlays" / "renders" / "overlay.mov"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\0" * 64)
        return _finish(S.RENDER_OVERLAY, config,
                       {"ok": True, "output": str(out), "stub": True})(state)

    return fn


# ============================================================================
#  剧本
# ============================================================================
@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    blurb: str          # 一句话说清"这个剧本要让人看到什么"
    script: Callable[[], dict]     # 每次取用返回**新的一份**（队列会被 pop）


SCENARIOS: dict[str, Scenario] = {
    s.key: s for s in (
        Scenario(
            "happy", "全链通过",
            "19 个阶段依次跑完、直接交付。看的是正常路径与每步的归属。",
            lambda: {},
        ),
        Scenario(
            "retry", "失败 → 回退 → 继续",
            "剪切被内核以「删了幻觉嫌疑段」拒绝 ⇒ 回退到剪裁策划重做判断，第二次通过。"
            "看的是回退目标与回退记账。",
            lambda: {"cut": [_FAIL("kb_cut", "hallucination_cut_forbidden",
                                   hint="把这些下标从删除列表里移除，或走例外放行")]},
        ),
        Scenario(
            "srt_fix", "工具级修复（不惊动模型）",
            "片尾质检查出字幕缺陷 ⇒ 只重跑「生成字幕」这一步。"
            "看的是「能修的绝不叫模型重做」。",
            lambda: {"qc:final": [_FAIL("kb_qc", "srt_bom", causes=["srt_bom"])]},
        ),
        Scenario(
            "paid", "付费闸门",
            "BGM 需要付费 ⇒ 流程**停在中转**等你拍板，未批准前一次付费调用都不发生。"
            "批准后才继续，拒绝就停。",
            lambda: {"bgm:probe": [_FAIL("kb_bgm", "bgm_needs_confirm")]},
        ),
        Scenario(
            "guard", "护栏：同因连续 2 次即停",
            "同一 cause 连续两次未解决 ⇒ 按护栏停止重试（**不是**再试第三次），"
            "并如实报出现场。",
            lambda: {"cut": [_FAIL("kb_cut", "hallucination_cut_forbidden"),
                             _FAIL("kb_cut", "hallucination_cut_forbidden")]},
        ),
        Scenario(
            "resume", "断点续跑",
            "内核说「字幕档已经在了」⇒ 前面的转写/剪裁全部跳过，从第 6 步接续。"
            "看的是「先问状态，再动」。",
            lambda: {"status": [_OK("kb_status", next_stage="3_srt")]},
        ),
    )
}


def scenario_list() -> list[dict]:
    return [{"key": s.key, "title": s.title, "blurb": s.blurb} for s in SCENARIOS.values()]


# ============================================================================
#  装配
# ============================================================================
def fresh_project(config: AppConfig, scenario_key: str) -> Path:
    """给演练开一个项目目录 —— **每个剧本一个固定目录**。

    为什么固定而不是每次新建再删旧的：
      · 一次演练会经历"跑到中断 → 你拍板 → 再恢复"两轮，两轮必须看到同一份盘上文件
        （`edit/assemble.json`、`overlays/renders/*.mov` 是第二轮要读的）；
      · 桩写的那几个文件（三份规格、`index.html`、`overlay.mov`）每轮都会**覆盖重写**，
        根本不需要"先清空"；
      · 而"清空"意味着删除 —— 删除是不可逆动作。这里既然没有必须清空的理由，
        就不该引入一个删除路径（环境里也有批量删除护栏，绕过它更是错的方向）。

    每次运行之间靠 **thread_id 带 nonce** 来隔离（状态在检查点库里按 thread 存），
    目录复用完全不影响"从零开始"这个语义。
    """
    root = Path(config.projects_root) / REHEARSAL_DIR / scenario_key
    for sub in ("edit", "renders", "overlays/renders", "bgm", "input"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def prune_old_projects(config: AppConfig, keep: int = 8, cap: int = 3) -> int:
    """清掉**旧方案遗留**的带 nonce 目录（`<剧本>_<nonce>/`）。

    只删自己生成的脚手架，且限定在 `_rehearsal/` 之内、每论最多删 `cap` 个 ——
    这不是洁癖，而是"删除要有闸门"的体现：一次删几百个目录和一次删 3 个，
    前者一旦路径算错就是灾难，后者最坏也只是少几个占位文件。

    任何一种失败都不往外抛：清理失败绝不能连带把演练搞挂。
    """
    root = Path(config.projects_root) / REHEARSAL_DIR
    if not root.is_dir():
        return 0
    known = set(SCENARIOS)
    legacy = []
    for d in root.iterdir():
        if not d.is_dir() or d.name in known:
            continue
        # 只认 `<已知剧本>_<8位hex>` 这个形状，其余一律不碰
        name, _, nonce = d.name.rpartition("_")
        if name in known and len(nonce) == 8 and all(c in "0123456789abcdef" for c in nonce):
            legacy.append(d)
    removed = 0
    for d in sorted(legacy, key=lambda x: x.stat().st_mtime, reverse=True)[cap:]:
        try:
            shutil.rmtree(d)
            removed += 1
        except Exception:  # noqa: BLE001 —— 删不掉就算了，不值得让调用方失败
            break
    return removed


def build_rehearsal_app(config: AppConfig, scenario_key: str, checkpointer):
    """编译**演练版**图：真节点 + 桩内核 + 桩判断节点。"""
    scenario = SCENARIOS.get(scenario_key) or SCENARIOS["happy"]
    client = StubKernel(scenario.script())

    overrides: dict[str, NodeFn] = {
        S.PLAN_CUT: _plan_cut(config),
        S.PLAN_SUBTITLES: _judge(S.PLAN_SUBTITLES, config),
        S.REVIEW_TEST: _judge(S.REVIEW_TEST, config),
        S.REVIEW_FINAL: _judge(S.REVIEW_FINAL, config),
        # 这三个的下游契约是"留下文件"，桩必须照做
        S.PLAN_FINAL: _plan_final(config),
        S.DESIGN_OVERLAY: _design_overlay(config),
        S.RENDER_OVERLAY: _render_overlay(config),
    }
    nodes = build_nodes(client, config, llm=False, overrides=overrides)
    return build_graph(config, client=client, nodes=nodes, checkpointer=checkpointer)
