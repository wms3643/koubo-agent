"""判断节点的回归 —— 用**假模型**，不联网、不花钱。

覆盖三件事（前两件是这一轮新加的逻辑，也是最容易悄悄失效的地方）：

    一、**失败反馈真的进了提示词**
        没有它，回退就是"把同一个错误答案再答一遍"。所以这里直接断言
        "重试时发出的提示词里含上一轮的内核报错与提示"。
    二、**输出契约预检**
        缺字段 / 下标越界 / 类型不对 ⇒ 必须变成 `llm_bad_output`（原地重问一次），
        而不是被当成"空决策"**静默通过**（那就是不删任何段 —— 最危险的静默错误）。
    三、**证据与产物**
        决策文件真的按模型输出写对了；动效工程按标记原文落盘；审片把帧图带上了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FAIL, FakeClient, FakeLLM, seg, write_plan_context  # noqa: E402

from langgraph_app import stages as S  # noqa: E402
from langgraph_app.config import AppConfig  # noqa: E402
from langgraph_app.llm import parse_json  # noqa: E402
from langgraph_app.nodes import llm_nodes  # noqa: E402
from langgraph_app.state import initial_state  # noqa: E402


@pytest.fixture()
def config(tmp_path) -> AppConfig:
    return AppConfig.from_env(projects_root=tmp_path,
                              checkpoint_db=tmp_path / ".state" / "ck.sqlite")


@pytest.fixture()
def use_llm(monkeypatch):
    """把 `build_chat` 换成假模型，并把它交回给测试断言提示词。"""
    def _install(reply: str, *, model: type = FakeLLM):
        fake = model(reply)
        monkeypatch.setattr(llm_nodes, "build_chat", lambda *a, **k: fake)
        return fake

    return _install


def state_with(tmp_path: Path, **kw):
    st = initial_state(str(tmp_path))
    st.update(kw)
    return st


# ===========================================================================
#  一、失败反馈回路
# ===========================================================================
def test_retry_feeds_previous_failure_back_to_the_model(config, tmp_path, use_llm):
    """★ 核心：回退到剪裁策划时，提示词里必须带着上一轮**内核的**报错与提示。"""
    write_plan_context(tmp_path, [seg(0, "第一句"), seg(1, "第二句")])
    fake = use_llm(json.dumps({"drop_segment_ids": [], "notes": "空"}))

    # 造一个"上一次 APPLY_CUT 失败、回退目标是 PLAN_CUT"的状态
    st = state_with(
        tmp_path,
        results={S.APPLY_CUT: {"tool": "kb_cut", "ok": False, "cause": "hallucination_cut_forbidden",
                               "report": {"ok": False,
                                          "cause": "hallucination_cut_forbidden",
                                          "error": "删除集里含 2 个幻觉嫌疑段: [3, 7]",
                                          "hint": "把它们从 drop_segment_ids 里移除",
                                          "segments": [{"idx": 3, "text": "嗯嗯嗯"}]}}},
        route_history=[{"node": S.APPLY_CUT, "ok": False,
                        "causes": ["hallucination_cut_forbidden"],
                        "goto": S.PLAN_CUT, "kind": "rethink", "cost": "low",
                        "reason": "回退到剪裁策划"}],
        cause_history=["hallucination_cut_forbidden"],
        retakes={S.PLAN_CUT: 1},   # 回退账本里已经记了一次 ⇒ 这次是第 2 次尝试
    )
    cmd = llm_nodes.make_plan_cut(FakeClient(), config)(st)

    prompt = fake.prompts[-1]
    assert "上一轮失败反馈" in prompt
    assert "hallucination_cut_forbidden" in prompt
    assert "删除集里含 2 个幻觉嫌疑段" in prompt, "内核的原文报错必须背回去"
    assert "把它们从 drop_segment_ids 里移除" in prompt, "内核的提示也要背回去"
    assert "第 2 次尝试" in prompt

    # 至于结果：去掉非法下标之后照常写文件
    written = json.loads((tmp_path / "edit" / "edit_decisions.json").read_text("utf-8"))
    assert written["drop_segment_ids"] == []
    assert cmd.goto == S.APPLY_CUT


def test_first_attempt_has_no_feedback_block(config, tmp_path, use_llm):
    write_plan_context(tmp_path, [seg(0, "第一句")])
    fake = use_llm(json.dumps({"drop_segment_ids": []}))
    llm_nodes.make_plan_cut(FakeClient(), config)(state_with(tmp_path))
    assert "上一轮失败反馈" not in fake.prompts[-1]


def test_feedback_only_when_routed_here(config, tmp_path, use_llm):
    """失败的回退目标**不是**本节点时，不该喂无关的失败信息。"""
    write_plan_context(tmp_path, [seg(0, "第一句")])
    fake = use_llm(json.dumps({"drop_segment_ids": []}))
    st = state_with(
        tmp_path,
        route_history=[{"node": S.RUN_QC, "ok": False, "causes": ["srt_bom"],
                        "goto": S.BUILD_SRT, "kind": "fix", "cost": "low", "reason": "重跑 srt"}],
        cause_history=["srt_bom"],
    )
    llm_nodes.make_plan_cut(FakeClient(), config)(st)
    assert "上一轮失败反馈" not in fake.prompts[-1]


# ===========================================================================
#  二、输出契约预检（关键是"不能静默通过"）
# ===========================================================================
def test_missing_drop_key_is_bad_output(config, tmp_path, use_llm):
    write_plan_context(tmp_path, [seg(0, "第一句")])
    use_llm(json.dumps({"notes": "我只写了个 notes"}))
    cmd = llm_nodes.make_plan_cut(FakeClient(), config)(state_with(tmp_path))
    assert cmd.update["results"][S.PLAN_CUT]["cause"] == "llm_bad_output"
    # 关键：**没有**写出决策文件 —— 否则内核会按"不删任何段"继续跑
    assert not (tmp_path / "edit" / "edit_decisions.json").exists()


def test_out_of_range_index_is_bad_output_with_range_hint(config, tmp_path, use_llm):
    write_plan_context(tmp_path, [seg(0, "甲"), seg(1, "乙")])
    use_llm(json.dumps({"drop_segment_ids": [0, 99]}))
    cmd = llm_nodes.make_plan_cut(FakeClient(), config)(state_with(tmp_path))
    err = cmd.update["results"][S.PLAN_CUT]["report"]["error"]
    assert "越界" in err and "0..1" in err, f"要直接把合法范围告诉模型，实际：{err}"
    assert not (tmp_path / "edit" / "edit_decisions.json").exists()


def test_non_integer_index_is_bad_output(config, tmp_path, use_llm):
    write_plan_context(tmp_path, [seg(0, "甲")])
    use_llm(json.dumps({"drop_segment_ids": ["idx-3"]}))
    cmd = llm_nodes.make_plan_cut(FakeClient(), config)(state_with(tmp_path))
    assert cmd.update["results"][S.PLAN_CUT]["cause"] == "llm_bad_output"


def test_bad_json_is_bad_output(config, tmp_path, use_llm):
    write_plan_context(tmp_path, [seg(0, "甲")])
    use_llm("我建议删掉第 3 段。")   # 只有自然语言，没有 JSON
    cmd = llm_nodes.make_plan_cut(FakeClient(), config)(state_with(tmp_path))
    assert cmd.update["results"][S.PLAN_CUT]["cause"] == "llm_bad_output"


def test_plan_cut_writes_source_as_video_path_not_author(config, tmp_path, use_llm):
    """★ `edit_decisions.json.source` 是**源视频路径**，不是"谁写的这个文件"。

    真跑撞到过：我写成 `"langgraph:plan_cut"`，内核 stage2 拿它当 ffmpeg 的 `-i` 输入
    （`decisions.get("source") or "input/raw.mp4"`）⇒ 去读一个不存在的文件 ⇒
    cut-apply 直接崩（exit 127）。契约 §二 只列了字段名、没写含义，所以这条测试
    守的是"别再犯同一个误读"。
    """
    write_plan_context(tmp_path, [seg(0, "甲")])
    use_llm(json.dumps({"drop_segment_ids": []}))
    llm_nodes.make_plan_cut(FakeClient(), config)(state_with(tmp_path))

    written = json.loads((tmp_path / "edit" / "edit_decisions.json").read_text("utf-8"))
    assert written["source"] == "input/raw.mp4"
    assert "langgraph" not in written["source"], "别把作者名塞进内核消费的字段"
    assert written["authored_by"].startswith("langgraph:"), "作者信息另开一个键"


def test_valid_drop_list_is_written(config, tmp_path, use_llm):
    write_plan_context(tmp_path, [seg(0, "试讲"), seg(1, "好"), seg(2, "试讲")])
    use_llm(json.dumps({"drop_segment_ids": [2, 0, 0], "text_fixes": {"1": "好"},
                        "notes": "删试讲"}))
    cmd = llm_nodes.make_plan_cut(FakeClient(), config)(state_with(tmp_path))
    written = json.loads((tmp_path / "edit" / "edit_decisions.json").read_text("utf-8"))
    assert written["drop_segment_ids"] == [0, 2], "去重并排序"
    assert written["text_fixes"] == {"1": "好"}
    assert cmd.goto == S.APPLY_CUT


# ===========================================================================
#  字幕制作
# ===========================================================================
def _write_kept(project: Path) -> None:
    from langgraph_app.nodes.base import write_json

    write_json(project / "edit" / "transcript.json",
               [{"start": 0, "end": 1, "text": "第一句"}, {"start": 1, "end": 2, "text": "第二句"}])
    # ⚠️ first_seg/last_seg 是 **1-based**（全项目唯一例外）
    write_json(project / "edit" / "cut_blocks.json",
               [{"first_seg": 1, "last_seg": 2, "edit_start": 0, "edit_end": 2,
                 "orig_start": 0, "orig_end": 2}])


def test_subtitles_writes_fixes(config, tmp_path, use_llm):
    _write_kept(tmp_path)
    use_llm(json.dumps({"text_fixes": {"1": "第二局"}}))
    cmd = llm_nodes.make_plan_subtitles(FakeClient(), config)(state_with(tmp_path))
    written = json.loads((tmp_path / "edit" / "subtitle_fixes.json").read_text("utf-8"))
    assert written["text_fixes"] == {"1": "第二局"}
    assert cmd.goto == S.BUILD_SRT


def test_subtitles_cross_deleted_goes_back_to_cut_planner(config, tmp_path, use_llm):
    _write_kept(tmp_path)
    use_llm(json.dumps({"cross_deleted_sentence": True, "cross_detail": "idx 1 被切断"}))
    cmd = llm_nodes.make_plan_subtitles(FakeClient(), config)(state_with(tmp_path))
    assert cmd.goto == S.PLAN_CUT, "跨删除区是**保留块**的问题，必须回剪裁策划"
    assert cmd.update["cross_agent_events"], "跨 agent 回路要记账（每片 ≤1）"
    assert not (tmp_path / "edit" / "subtitle_fixes.json").exists()


def test_subtitles_missing_kept_segments(config, tmp_path, use_llm):
    use_llm(json.dumps({"text_fixes": {}}))
    cmd = llm_nodes.make_plan_subtitles(FakeClient(), config)(state_with(tmp_path))
    assert cmd.update["results"][S.PLAN_SUBTITLES]["cause"] == "missing_file"


# ===========================================================================
#  特效设计
# ===========================================================================
def _write_timeline(project: Path) -> None:
    from langgraph_app.nodes.base import write_json

    write_json(project / "edit" / "timeline_map.json",
               {"edit_duration": 2.0,
                "segments": [{"edit_start": 0, "edit_end": 1, "text": "一",
                              "words": [{"w": "一", "s": 0.0, "e": 0.5}]}]})


# 一份**最小但合规**的 HyperFrames 工程（满足 lint 的四条硬要求）
VALID_OVERLAY_HTML = (
    '<!doctype html><html><body>'
    '<div id="root" data-composition-id="main" data-start="0" data-width="1920" '
    'data-height="1080" data-duration="6"></div>'
    '<script src="./vendor/gsap.min.js"></script>'
    "<script>const tl=gsap.timeline({paused:true});"
    'window.__timelines=window.__timelines||{};window.__timelines["main"]=tl;</script>'
    "</body></html>"
)


def test_overlay_written_from_marker(config, tmp_path, use_llm):
    _write_timeline(tmp_path)
    fake = use_llm(f"这是我的工程：\n<!--OVERLAY_HTML-->\n{VALID_OVERLAY_HTML}")
    cmd = llm_nodes.make_design_overlay(FakeClient(), config)(state_with(tmp_path))
    saved = (tmp_path / "overlays" / "index.html").read_text("utf-8")
    assert saved == VALID_OVERLAY_HTML, "标记之后要**原文**保存（不塞进 JSON，避免转义出错）"
    assert cmd.goto == S.RENDER_OVERLAY
    # 时间轴是"剪后"那份，绝不能是 transcript.json
    assert "剪后" in fake.prompts[-1]
    # 提示词必须把真实契约讲清楚（这些漏一条就会渲染出静止/空的 MOV）
    for must in ("data-composition-id", "__timelines", "paused", "vendor/gsap.min.js",
                 "只允许这些属性", "初始隐藏态写在 CSS"):
        assert must in fake.prompts[-1], f"提示词缺了契约要点：{must}"


def test_overlay_vendor_gsap_is_placed_by_code(config, tmp_path, use_llm):
    """提示词要求工程引本地 GSAP —— 那就必须由**代码**把库放到位（模型装不了包）。"""
    _write_timeline(tmp_path)
    use_llm(VALID_OVERLAY_HTML)
    cmd = llm_nodes.make_design_overlay(FakeClient(), config)(state_with(tmp_path))
    vendored = tmp_path / "overlays" / "vendor" / "gsap.min.js"
    assert vendored.is_file() and vendored.stat().st_size > 10000, "没把 GSAP 放进项目"
    assert any("本地动画库" in n for n in cmd.update.get("notes") or [])


def test_overlay_without_timeline_registry_is_rejected(config, tmp_path, use_llm):
    """⛔ 没有 `window.__timelines` 注册 ⇒ 运行时发现不了时间线 ⇒ 动画**根本不播**，
    而且渲染**不会报错**（只会安静产出静止画面）。必须在写文件前拦下。"""
    _write_timeline(tmp_path)
    bad = ('<!doctype html><html><body>'
           '<div id="root" data-composition-id="main" data-start="0" '
           'data-width="1920" data-height="1080" data-duration="6"></div>'
           '<script>const tl=gsap.timeline({paused:true});tl.to("#a",{opacity:1},0);</script>'
           '</body></html>')
    # 带上标记，隔离出"契约检查"这一道（否则会先被 HTML 提取拦住）
    use_llm(f"<!--OVERLAY_HTML-->\n{bad}")
    cmd = llm_nodes.make_design_overlay(FakeClient(), config)(state_with(tmp_path))
    res = cmd.update["results"][S.DESIGN_OVERLAY]
    assert res["cause"] == "llm_bad_output" and "__timelines" in res["report"]["error"]
    assert not (tmp_path / "overlays" / "index.html").exists()


def test_overlay_without_composition_id_is_rejected(config, tmp_path, use_llm):
    _write_timeline(tmp_path)
    use_llm('<!--OVERLAY_HTML-->\n<!doctype html><html><body>'
            '<div id="root"></div><script>window.__timelines={};</script></body></html>')
    cmd = llm_nodes.make_design_overlay(FakeClient(), config)(state_with(tmp_path))
    res = cmd.update["results"][S.DESIGN_OVERLAY]
    assert res["cause"] == "llm_bad_output" and "data-composition-id" in res["report"]["error"]


def test_overlay_without_html_is_bad_output(config, tmp_path, use_llm):
    _write_timeline(tmp_path)
    use_llm("我建议放一个淡入效果。")
    cmd = llm_nodes.make_design_overlay(FakeClient(), config)(state_with(tmp_path))
    assert cmd.update["results"][S.DESIGN_OVERLAY]["cause"] == "llm_bad_output"
    assert not (tmp_path / "overlays" / "index.html").exists()


def test_overlay_skipped_when_user_asks(config, tmp_path, use_llm):
    _write_timeline(tmp_path)
    use_llm("不该被调用")
    st = state_with(tmp_path, goal={"overlay_required": False})
    cmd = llm_nodes.make_design_overlay(FakeClient(), config)(st)
    assert cmd.goto == S.RENDER_OVERLAY
    assert cmd.update["results"][S.DESIGN_OVERLAY]["report"]["skipped"]


# ===========================================================================
#  成片规格
# ===========================================================================
def test_plan_final_writes_specs_with_deterministic_paths(config, tmp_path, use_llm):
    """LLM 只定「怎么混」；**文件路径由代码写死**。

    这条守的是"成片链路能不能接上"：路径一旦交给 LLM 猜，真跑会出现
    "成片永不产出"或"成片静默没有 BGM"（两者都是真跑暴露过的）。
    """
    _write_timeline(tmp_path)
    fake = use_llm(json.dumps({
        "bgm": {"style": "轻快的纯器乐钢琴，中速，商务感，不抢人声"},   # ≥20 字
        "mix": {"bgm_volume_sections": [{"start": 0, "end": 2, "volume": 0.1}],
                "bgm_fade_out": 1.5},
    }))
    cmd = llm_nodes.make_plan_final(FakeClient(), config)(state_with(tmp_path))

    # ⚠️ 提示词必须把音量判据的**两侧边界都讲清楚**。
    #    真跑撞到过：提示词只警告"偏大会抢戏"，示例值写 0.12 ⇒ 模型给出 0.2，
    #    实测 Δ 只有 +0.20 LU（门槛 0.3）⇒ 判 bgm_silent，整条片被卡住。
    #    这类"只讲一侧面"的提示词会稳定地把模型推向错误方向，所以要断言住。
    p = fake.prompts[-1]
    for must in ("0.3", "4.0", "bgm_silent", "bgm_overpowers_voice", "实用区间"):
        assert must in p, f"plan_final 提示词漏了音量判据要点：{must}"

    bgm = json.loads((tmp_path / "edit" / "bgm.json").read_text("utf-8"))
    mix = json.loads((tmp_path / "edit" / "mix.json").read_text("utf-8"))
    asm = json.loads((tmp_path / "edit" / "assemble.json").read_text("utf-8"))

    assert bgm["style"] and len(bgm["style"]) >= 20
    assert bgm["output"] == "bgm/bgm.mp3"
    assert mix["video"] == S.SKELETON_VIDEO, "mix 的主轨必须是**保留音频**的骨架"
    assert mix["output"] == S.MIXED_MASTER
    assert mix["bgm_volume_sections"][0]["volume"] == 0.1
    assert asm["segments"][0]["path"] == S.SUBTITLED_FULL
    assert asm["output"] == S.SKELETON_VIDEO
    assert cmd.goto == S.GENERATE_BGM


def test_plan_final_short_style_is_bad_output(config, tmp_path, use_llm):
    _write_timeline(tmp_path)
    use_llm(json.dumps({"bgm": {"style": "轻音乐"}, "mix": {}}))
    cmd = llm_nodes.make_plan_final(FakeClient(), config)(state_with(tmp_path))
    err = cmd.update["results"][S.PLAN_FINAL]["report"]["error"]
    assert "20" in err, "要说清是字数不够（付费护栏要求 ≥20 字）"
    assert not (tmp_path / "edit" / "bgm.json").exists()


# ===========================================================================
#  成片质检
# ===========================================================================
def _write_frames(project: Path, n: int = 3, *, prefix: str = "qc_") -> list[str]:
    """造帧图。`prefix` 要跟**审片节点认的前缀**对上：
    `burn-test` 产 `frame_*`（给 REVIEW_TEST），`qc` 产 `qc_*`（给 REVIEW_FINAL）。"""
    d = project / "edit" / "tmpimg"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = d / f"{prefix}00_{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        out.append(str(p))
    return out


def test_review_without_frames_is_probe_failure(config, tmp_path, use_llm):
    use_llm(json.dumps({"pass": True}))
    cmd = llm_nodes.make_llm_nodes(FakeClient(), config)[S.REVIEW_FINAL](state_with(tmp_path))
    assert cmd.update["results"][S.REVIEW_FINAL]["cause"] == "qc_probe_failed", \
        "没有证据不得当作通过"


def test_review_passes_and_sends_images(config, tmp_path, use_llm):
    _write_frames(tmp_path)
    fake = use_llm(json.dumps({"pass": True, "reasons": ["字幕已烧进画面"]}))
    cmd = llm_nodes.make_llm_nodes(FakeClient(), config)[S.REVIEW_FINAL](state_with(tmp_path))
    assert cmd.goto == S.DELIVER
    assert "<image:data:image/png;base64," in fake.prompts[-1], "帧图必须真的发给模型"


def test_review_fail_routes_via_matrix(config, tmp_path, use_llm):
    _write_frames(tmp_path, prefix="frame_")   # 测试片审片认的是 burn-test 的前缀
    use_llm(json.dumps({"pass": False, "cause": "subtitle_occlusion",
                        "margin_v": 20, "reasons": ["字幕压到下巴"]}))
    cmd = llm_nodes.make_llm_nodes(FakeClient(), config)[S.REVIEW_TEST](state_with(tmp_path))
    assert cmd.goto == S.BURN_TEST, "挡脸要重烧"
    assert cmd.update["revision"]["margin_v"] == 20, "★ 新 MarginV 必须带到重烧那一步"


def test_review_fail_with_illegal_target_halts(config, tmp_path, use_llm):
    _write_frames(tmp_path)
    use_llm(json.dumps({"pass": False, "cause": "我编的cause", "target": "halt"}))
    cmd = llm_nodes.make_llm_nodes(FakeClient(), config)[S.REVIEW_FINAL](state_with(tmp_path))
    assert cmd.goto == S.HALT, "看不懂就停下，不许放行"


def test_review_prefers_this_run_report_frames(config, tmp_path, use_llm):
    """帧图首选**本轮内核报告里给出的确切路径**（那是这次运行真正产出的）。"""
    fresh = _write_frames(tmp_path, 2)
    use_llm(json.dumps({"pass": True}))
    st = state_with(tmp_path, results={S.RUN_QC: {"report": {"frames": fresh}}})
    cmd = llm_nodes.make_llm_nodes(FakeClient(), config)[S.REVIEW_FINAL](st)
    assert cmd.update["review_frames"] == fresh
    assert cmd.update["review_frames_source"].startswith("本轮")
    assert "notes" not in cmd.update, "来源可信时不该产生噪音"


def test_review_fallback_ignores_stale_frames_from_other_stages(config, tmp_path, use_llm):
    """★ 兜底扫描绝不能把**上一轮/别的阶段**残留的帧当证据。

    `edit/tmpimg/` 跨阶段共用且旧帧不清理 —— 这是最容易被"看起来有证据"骗过去的地方。
    """
    qc_frames = _write_frames(tmp_path, 2)          # qc_*.png（本轮 qc 产的）
    tmpimg = tmp_path / "edit" / "tmpimg"
    stale = tmpimg / "frame_9.99s.png"              # 上一轮 burn-test 残留
    stale.write_bytes(b"\x89PNG\r\n\x1a\nstale")

    use_llm(json.dumps({"pass": True}))
    # 报告里**没有** frames ⇒ 只能走兜底
    cmd = llm_nodes.make_llm_nodes(FakeClient(), config)[S.REVIEW_FINAL](state_with(tmp_path))

    used = cmd.update["review_frames"]
    assert str(stale) not in used, "旧轮次的帧必须被排除（否则是拿不存在的证据下断言）"
    assert sorted(used) == sorted(qc_frames), f"只该认 qc_ 前缀，实际：{used}"
    # 而且必须**留痕**：来源有疑点要让用户看见
    assert cmd.update["review_frames_source"].startswith("⚠️ 兜底")
    assert any("帧图来源" in n for n in cmd.update.get("notes") or [])


def test_review_test_fallback_uses_burn_test_prefix(config, tmp_path, use_llm):
    """测试片审片认 `frame_` 前缀 —— 前缀对不上就等于没有证据。"""
    tmpimg = tmp_path / "edit" / "tmpimg"
    tmpimg.mkdir(parents=True, exist_ok=True)
    (tmpimg / "frame_1.50s.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (tmpimg / "qc_00_1.00s.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)

    use_llm(json.dumps({"pass": True}))
    cmd = llm_nodes.make_llm_nodes(FakeClient(), config)[S.REVIEW_TEST](state_with(tmp_path))
    used = [Path(p).name for p in cmd.update["review_frames"]]
    assert used == ["frame_1.50s.png"], f"实际用了 {used}"


# ===========================================================================
#  纯函数
# ===========================================================================
@pytest.mark.parametrize("text,expect_key", [
    ('{"a": 1}', "a"),
    ('```json\n{"a": 2}\n```', "a"),
    ('好的：\n{"a": 3}\n以上', "a"),
])
def test_parse_json_variants(text, expect_key):
    assert expect_key in parse_json(text)


def test_parse_json_rejects_non_object():
    from langgraph_app.llm import LLMError

    with pytest.raises(LLMError):
        parse_json("[1,2,3]")
    with pytest.raises(LLMError):
        parse_json("")


def test_extract_html_without_marker_or_fence():
    from langgraph_app.llm import LLMError, extract_html

    assert extract_html("<!--OVERLAY_HTML-->\n<p>x</p>") == "<p>x</p>"
    assert extract_html("```html\n<p>y</p>\n```") == "<p>y</p>"
    assert extract_html("<!doctype html><html></html>").startswith("<!doctype")
    with pytest.raises(LLMError):
        extract_html("就是一段普通文字")
