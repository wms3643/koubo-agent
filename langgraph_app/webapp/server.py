"""编排服务的 HTTP 接口、运行控制和 SSE 事件流。

流程状态通过 LangGraph 检查点读取，人工审批通过同一 thread_id 恢复。
单进程持有任务队列与运行锁，并发启动标准流程时返回 409。
检查点和会话记录由配置选择 SQLite 或 PostgreSQL；状态查询不调用模型。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.types import Command

from .. import routing as R
from .. import stages as S
from ..config import AppConfig
from ..graph import build_graph, make_checkpointer, open_checkpoint_db
from ..state import initial_state
from . import rehearsal as RH

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ============================================================================
#  节点表 —— 界面要讲人话，所以中文名与"这步在干什么"在这里定义一次
# ============================================================================
# ⚠️ 这份表是**展示用**的，不参与任何判断。名字本身的事实源仍然是 `stages.py`。
NODE_INFO: dict[str, tuple[str, str]] = {
    S.PROBE: ("问状态", "读内核断点：已经有哪些产物、该从哪一步接续（只读，不动手）"),
    S.OVERRIDE: ("例外放行", "主管独有：重转写 / 放行删幻觉段，必须写明理由"),
    S.TRANSCRIBE: ("转写", "GPU 逐字稿 + word 级时间戳"),
    S.PLAN_CUT: ("判断删留", "模型看逐字稿与证据，决定删哪些段、保留块怎么切"),
    S.APPLY_CUT: ("执行剪切", "按决策真的剪，并自检接缝"),
    S.PLAN_SUBTITLES: ("字幕修复判断", "模型按逐字稿纠正字幕文本"),
    S.BUILD_SRT: ("生成字幕", "生成 SRT 与剪后时间轴映射"),
    S.BURN_TEST: ("烧测试片", "只烧 10 秒，用来验字幕排版与挡脸"),
    S.REVIEW_TEST: ("看图审测试片", "质检看抓帧：字幕位置/可读性/挡脸"),
    S.BURN_FULL: ("烧全片", "把字幕烧进全片"),
    S.DESIGN_OVERLAY: ("设计动效", "模型写动效工程 index.html（GSAP 时间轴）"),
    S.RENDER_OVERLAY: ("渲染动效", "把动效工程渲成带 alpha 的 MOV"),
    S.PLAN_FINAL: ("定成片规格", "写出 bgm / mix / assemble 三份规格"),
    S.GENERATE_BGM: ("生成 BGM", "唯一花钱的一步，付费前必然停下来问人"),
    S.ASSEMBLE_BASE: ("缝接骨架", "把保留块缝成一条主轨"),
    S.MIX_BGM: ("铺 BGM", "把 BGM 混进人声主轨 → 无特效版成片"),
    S.APPLY_OVERLAY: ("叠加动效", "叠动效层 → 唯一交付物 renders/final.mp4"),
    S.RUN_QC: ("片尾质检", "对成片跑数字断言（含抓帧）"),
    S.REVIEW_FINAL: ("成片审片", "质检看抓帧 + 复核数字断言"),
    S.DELIVER: ("交付", "给出成片路径、规格与每个 cause 的处理记录"),
    S.HALT: ("停下", "如实报告：为什么停、卡在哪、还差什么"),
    S.APPROVAL: ("请人拍板", "唯一使用 interrupt() 的节点；只有读操作，重跑无副作用"),
}


def _node_row(name: str, kind: str = "step") -> dict:
    label, doing = NODE_INFO.get(name, (name, ""))
    return {
        "id": name,
        "label": label,
        "doing": doing,
        "owner": S.OWNER.get(name, "—"),
        "worker": name in S.WORKER_NODES,
        "kind": kind,      # step=主链一步 / gate=闸门 / terminal=终态
    }


# 画在界面上的**真实链序**。刻意按链序而不是按 agent 分组：
# 分组会让人看不出"谁在什么时候干活"；链序 + 颜色标识归属，两者都保住了。
# `override` 与 `request_approval` 不在 `stages.CHAIN` 里（它们不是主链的一步），
# 但在真实运行里确实会出现，所以显式插进展示顺序。
DISPLAY_CHAIN: tuple[tuple[str, str], ...] = (
    (S.PROBE, "step"),
    (S.OVERRIDE, "gate"),
    *((n, "step") for n in S.CHAIN[1:]),
    (S.APPROVAL, "gate"),
    (S.HALT, "terminal"),
)


# ============================================================================
#  密钥解析（不落盘、不进代码）
# ============================================================================
KEY_ENV_NAMES = ("LK_API_KEY", "YUANQI_API_KEY")
_KEY_DOC = Path.home() / "Desktop" / "元气中转站调用文档.md"
_KEY_RE = re.compile(r"API\s*Key\s*[:：]\s*`?([A-Za-z0-9\-_]{16,})`?")


def resolve_api_key() -> tuple[str | None, str]:
    """返回 `(密钥, 来源说明)`。来源会显示在界面上，让人知道自己在用哪把 Key。"""
    for name in KEY_ENV_NAMES:
        val = os.environ.get(name)
        if val:
            return val, f"环境变量 {name}"
    doc = Path(os.environ.get("KB_KEY_DOC") or _KEY_DOC)
    if doc.is_file():
        try:
            m = _KEY_RE.search(doc.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1), f"现场读取 {doc.name}"
        except OSError:
            pass
    return None, "未找到"


def _config() -> AppConfig:
    key, _ = resolve_api_key()
    cfg = AppConfig.from_env()
    if key:
        from dataclasses import replace
        cfg = replace(cfg, model=replace(cfg.model, api_key=key))
    return cfg


def resolve_project(cfg: AppConfig, project: str) -> Path:
    """把用户给的"项目"归一化。

    界面给的是**目录名**（`_real_person`），命令行给的是相对路径（`projects/_real_person`）——
    两种都得能用，而且两者都要**受 `projects_root` 约束**（那是 `resolve_project_dir`
    的断言，不能因为图方便就绕过去）。
    """
    p = Path(project).expanduser()
    if not p.is_absolute():
        p = Path(cfg.projects_root) / p
    return cfg.resolve_project_dir(p)


# ============================================================================
#  上传源视频
# ============================================================================
# 允许的容器。内核只要求 ffmpeg 能解；这里列常见几种，收了别的也只会在 stage1 才炸。
VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".flv"})
# Windows 保留名：真建出来会得到一个打不开的目录
_WIN_RESERVED = frozenset(
    {"con", "prn", "aux", "nul",
     *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
# 一次完整跑（转写→剪切→烧字→动效→混音）实测占几百 MB 到 1 GB（见 projects/ 现有项目：
# `_real_person` 262 MB、`_tts_lecture` 499 MB）。留 2 GB 余量再开跑，
# 免得跑到一半 C 盘满 —— 那时 ffmpeg 报的错会指向一个完全无关的地方。
RUN_HEADROOM_BYTES = 2 * 1024 ** 3
# 低于这个值直接拒绝上传（连传都传不进去就没意义）
MIN_FREE_BYTES = 512 * 1024 ** 2

_NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 上传的**暂存区**（在 projects_root 之内，所以同盘、`os.replace` 是原子的）。
# 为什么不直接往目标项目里写：上传要边写边验，而"验不过"是常态（选错文件、录屏没开麦）。
# 若先建项目再验，失败就会留下一个**没有 raw.mp4 的空项目**，它会出现在项目列表里，
# 看起来像个正经项目。先暂存、验过再建项目 ⇒ 被拒时盘上什么痕迹都没有。
_INCOMING = "_incoming"


def sanitize_project_name(raw: str, fallback: str) -> str:
    """把用户给的/从文件名推出来的项目名，变成**一定能建目录**的名字。

    规则宁可严一点：项目名要进文件路径、进 URL、进 JSON 报告。放行一个含 `..`
    或结尾带点的名字，代价是"文件落到别处"或"Windows 上目录打不开"这类难查的事故。
    """
    name = (raw or "").strip() or fallback
    name = _NAME_BAD.sub("_", name)
    name = name.strip().strip(".")          # Windows 不允许结尾是点/空格
    name = re.sub(r"\s+", "_", name)
    if not name:
        name = "project"
    if name.lower() in _WIN_RESERVED:
        name = f"{name}_project"
    return name[:48]


def unique_project_dir(root: Path, name: str) -> Path:
    """不覆盖已有项目 —— 重名就加 `-2` / `-3`。

    为什么不问"要不要覆盖"：覆盖一个项目的 `raw.mp4` 会让它下面**全部产物失效**
    （逐字稿/剪辑决策/时间轴/字幕都是按旧源片算的），而界面上一句"确认覆盖"
    拦不住这件事的后果。加后缀是无损的，用户想改名字自己改。
    """
    cand = root / name
    if not cand.exists():
        return cand
    for i in range(2, 200):
        cand = root / f"{name}-{i}"
        if not cand.exists():
            return cand
    raise HTTPException(409, f"{name} 及其后续编号都已存在，换个名字。")


def _free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ============================================================================
#  运行的登记与执行
# ============================================================================
@dataclass
class Run:
    id: str
    mode: str                       # real | rehearsal
    scenario: str | None
    project_dir: str
    thread_id: str
    input_kind: str                 # start | resume | status
    approve: bool | None
    goal: dict
    q: "queue.Queue[dict | None]" = field(default_factory=queue.Queue)
    started_at: float = field(default_factory=time.time)
    finished: bool = False
    error: str | None = None
    events: int = 0
    halted: bool = False
    waiting: bool = False


_RUNS: dict[str, Run] = {}
_RUNS_LOCK = threading.Lock()
_BUSY = threading.Lock()            # 全局"只允许一个运行"的闸门
_RUN_ORDER: list[str] = []
MAX_KEEP_RUNS = 40

# thread → 项目目录。恢复执行时要凭 thread 找回**原来那个**项目目录 ——
# 演练的目录带 nonce，光靠 scenario 是找不回来的；真实项目虽然能从项目名推，
# 但"推出来"和"记下来"在出错时差别很大（推错会静默作用到另一个项目上）。
_THREAD_DIR: dict[str, str] = {}
MAX_KEEP_THREADS = 60


def _remember_thread(thread_id: str, pdir: Path) -> None:
    _THREAD_DIR[thread_id] = str(pdir)
    while len(_THREAD_DIR) > MAX_KEEP_THREADS:
        _THREAD_DIR.pop(next(iter(_THREAD_DIR)))


def _put(run: Run, ev: dict) -> None:
    run.events += 1
    run.q.put(ev)


def _log(run: Run, text: str, level: str = "info") -> None:
    _put(run, {"type": "log", "level": level, "text": text,
               "t": round(time.time() - run.started_at, 2)})


def _readonly_nodes(config: AppConfig) -> dict:
    """一套"哑节点"：只为了让图能编译出来，好拿到 checkpointer 的**读**能力。

    `make_llm_nodes` 在构造时就 `build_chat()`（缺密钥直接抛），而"看状态"根本不需要模型。
    用哑节点编译一张同构的图，`get_state()` 读到的就是同一份检查点。
    """
    from ..nodes import build_nodes
    from ..nodes.base import command, synthetic
    from .. import routing as R

    llm_nodes = (S.PLAN_CUT, S.PLAN_SUBTITLES, S.DESIGN_OVERLAY, S.PLAN_FINAL,
                 S.REVIEW_TEST, S.REVIEW_FINAL)
    overrides = {}
    for name in llm_nodes:
        def fn(state, _n=name):
            goto, update = R.advance(node=_n, result=synthetic(_n, ok=True),
                                     state=state, config=config)
            return command(goto, update)
        overrides[name] = fn
    return build_nodes(None, config, llm=False, overrides=overrides)


def read_state(config: AppConfig, project_dir: Path, thread_id: str) -> dict:
    """读某个 thread 的检查点快照（含挂起的中断）。不需要密钥、不推进任何东西。

    ⚠️ 没有图断点时**再去问一次内核**（与 `run.py --status` 同一口径）：
    "这个项目现在到哪一步了"这个问题，内核自己就能答，而且那是它的事实源。
    对新项目只报"没有检查点"等于什么都没说 —— 用户要看的是项目实况。
    """
    conn = open_checkpoint_db(config)
    try:
        app = build_graph(config, nodes=_readonly_nodes(config),
                          checkpointer=make_checkpointer(conn))
        cfg = {"configurable": {"thread_id": thread_id}}
        snap = app.get_state(cfg)
        values = (snap.values if snap else {}) or {}
        out = {
            "thread_id": thread_id,
            "has_checkpoint": bool(values),
            "next": list(getattr(snap, "next", ()) or ()),
            "interrupts": [_interrupt_value(i) for i in
                           (getattr(snap, "interrupts", ()) or ())],
            "brief": _brief(values),
            "kernel": None,
        }
        if not values:
            out["kernel"] = _kernel_probe(config, project_dir)
        return out
    finally:
        conn.close()


def _kernel_probe(config: AppConfig, project_dir: Path) -> dict:
    """免费问一次内核状态。失败也返回结构（把 cause 一并交出去），不抛。"""
    from ..kernel import KernelClient

    try:
        r = KernelClient(config).status(project_dir)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "cause": f"{type(e).__name__}", "error": str(e)}
    rep = r.report or {}
    return {
        "ok": r.ok,
        "cause": r.cause,
        "next_stage": rep.get("next_stage"),
        "last_qc_ok": rep.get("last_qc_ok"),
        "last_qc_causes": rep.get("last_qc_causes") or [],
        "segment_count": rep.get("segment_count"),
        "resume_node": S.resume_node(rep.get("next_stage")) if r.ok else None,
        "artifacts_present": sorted(
            k for k, v in (rep.get("artifacts") or {}).items() if (v or {}).get("exists")),
        "error": rep.get("error"),
    }


def _interrupt_value(it) -> dict:
    value = getattr(it, "value", it)
    return value if isinstance(value, dict) else {"raw": str(value)}


def _brief(values: dict) -> dict:
    """状态摘要 —— 给界面看的小体积快照。

    ⚠️ 刻意**不把整个 state 打出去**：里面有大段内核报告（最长的一次状态几 MB），
    每推一步都发一遍会让界面变成"在等网络"。判读要用的字段这里都有；
    要看完整报告的人去 `edit/pipeline_report.json`，那才是权威现场。
    """
    if not values:
        return {}
    results = values.get("results") or {}
    route = list(values.get("route_history") or [])
    return {
        "project": values.get("project"),
        "resume_from": values.get("resume_from"),
        "results": {
            n: {
                "ok": bool(d.get("ok")),
                "cause": d.get("cause"),
                "causes": list(d.get("causes") or []),
                "summary": d.get("summary"),
                "duration_s": d.get("duration_s"),
                "skipped": (d.get("report") or {}).get("skipped"),
                "tool": d.get("tool"),
            }
            for n, d in results.items()
        },
        "retakes": dict(values.get("retakes") or {}),
        "cause_history": list(values.get("cause_history") or []),
        "route_history": route[-80:],
        "route_total": len(route),
        "notes": list(values.get("notes") or []),
        "halts": list(values.get("halts") or []),
        "approvals": dict(values.get("approvals") or {}),
        "pending_approval": dict(values.get("pending_approval") or {}),
        "cross_agent_events": list(values.get("cross_agent_events") or []),
        "goal": dict(values.get("goal") or {}),
        "kernel_status": {
            k: (values.get("status") or {}).get(k)
            for k in ("next_stage", "last_qc_ok", "last_qc_causes", "segment_count")
        },
        "final": values.get("final") or {},
    }


# 状态里"追加型"的键（与 `state.py` 里的 reducer 一一对应）；其余字典都是合并型
_LIST_KEYS = ("cause_history", "route_history", "notes", "halts", "cross_agent_events")


def _merge(acc: dict, upd: dict) -> None:
    """把节点的增量按 reducers 的语义并进本地视图（追加型列表 / 合并型字典）。"""
    for k, v in (upd or {}).items():
        if k in _LIST_KEYS and isinstance(v, (list, tuple)):
            acc[k] = list(acc.get(k) or []) + list(v)
        elif isinstance(v, dict):
            acc[k] = {**(acc.get(k) or {}), **v}
        else:
            acc[k] = v


def _next_of(node: str, upd: dict, record: dict, acc: dict, pending_before: dict) -> str | None:
    """这一步之后该谁跑。

    为什么不用 `get_state().next`：在 `stream()` 过程中它读到的是**上一次落盘的
    检查点**，会滞后一到两步 —— 实测表现为整条链推下去时 node_start 张冠李戴
    （node_end 全对，因为那是流里直接给的）。节点返回的 `Command(goto)` 是它自己
    声明的去向，`route_history` 里带着，这才是一手信息。

    四种形态各有出处：
      · 终态（deliver / halt）→ 没有下一个，图到头了
      · `probe` → **永远是例外放行闸门**。它 `route_history` 里记的 `goto` 是
        "续跑点"（语义），不是下一站 —— 中间刻意插了一道 `override`，
        因为例外放行会**改变续跑的意义**（重转写让剪辑/时间轴/字幕全作废）。
        这个区别不能弄混：照 `goto` 走会把 override 整步吞掉。
      · 产生待办（`pending_approval`）→ 去审批节点。这是**唯一**绕过
        `advance()` 直接返回 Command 的地方（见 `kernel_nodes.generate_bgm`），
        所以它没有 route_history 可查，只能认这个信号。
      · 普通节点 → `route_history[-1].goto`（`advance()` 记的账）
        ⚠️ 它是**裁决口径**的 goto，"停下"记成 `human`，要过 `graph_target` 翻成 `halt`
      · 例外放行节点透明通过时 → 跟着 `resume_from`（它就是"续跑点"本身）
      · 审批节点 → 批准去 `pending_approval.resume_target`，拒绝去 `halt`
    """
    if node in (S.DELIVER, S.HALT):
        return None
    if node == S.PROBE:
        return S.OVERRIDE
    if upd.get("pending_approval"):
        return S.APPROVAL
    if record and record.get("goto"):
        return R.graph_target(str(record["goto"]))
    if node == S.OVERRIDE:
        return acc.get("resume_from") or S.HALT
    if node == S.APPROVAL:
        if upd.get("halts"):
            return S.HALT
        if upd.get("approvals"):
            return pending_before.get("resume_target") or S.HALT
        return S.HALT
    return S.FOLLOWING.get(node)


def _execute(run: Run, config: AppConfig) -> None:
    """后台线程主体：编译图 → 推进 → 把每一步推进队列。"""
    conn = None
    try:
        conn = open_checkpoint_db(config)
        checkpointer = make_checkpointer(conn)

        if run.mode == "rehearsal":
            app = RH.build_rehearsal_app(config, run.scenario or "happy", checkpointer)
        else:
            app = build_graph(config, checkpointer=checkpointer)

        cfg = {"configurable": {"thread_id": run.thread_id}}

        if run.input_kind == "resume":
            payload: Any = Command(resume=run.approve)
            _log(run, f"人工答复：{'批准' if run.approve else '拒绝'} ⇒ 在同一 thread 上恢复",
                 "human")
        else:
            payload = initial_state(run.project_dir, goal=run.goal)
            _log(run, f"新建运行：{Path(run.project_dir).name}（thread={run.thread_id}）")

        _log(run, f"输入：{json.dumps(run.goal, ensure_ascii=False)}", "dim")

        # ---- 第一个节点 --------------------------------------------------
        # ⚠️ 恢复运行时**不是**从 probe 开始，而是重跑那个被中断的节点
        #    （LangGraph 恢复时把节点函数从头重跑，见 `stages.APPROVAL` 那段说明）。
        #    所以入口要问图 "还欠哪些任务"，不能写死 probe。
        snap0 = app.get_state(cfg)
        pending0 = [n for n in (getattr(snap0, "next", ()) or ()) if n != "__start__"]
        entry = pending0[0] if pending0 else S.PROBE
        _put(run, {"type": "node_start", "node": entry, **_node_row(entry)})

        # 累积一份"我自己的状态视图"：节点返回的 update 就是它写进状态的增量，
        # 照 reducers 合并即可。这样"下一步去哪"完全由节点自己报的 `goto` 决定，
        # 不依赖 LangGraph 内部读数（`get_state().next` 在流式过程中会滞后，
        # 实测过：整条链的 node_start 会推错，而 node_end 全是对的）。
        acc: dict = {}
        pending_before = dict(((snap0.values if snap0 else {}) or {}).get("pending_approval") or {})

        for chunk in app.stream(payload, cfg, stream_mode="updates"):
            for node, upd in (chunk or {}).items():
                if node == "__interrupt__":
                    for it in (upd or ()):
                        _put(run, {"type": "interrupt", "payload": _interrupt_value(it)})
                    continue

                upd = upd or {}
                data = (upd.get("results") or {}).get(node) or {}
                record = {}
                for rec in (upd.get("route_history") or []):
                    if rec.get("node") == node:
                        record = rec
                _put(run, {
                    "type": "node_end",
                    "node": node,
                    "label": NODE_INFO.get(node, (node, ""))[0],
                    "owner": S.OWNER.get(node, "—"),
                    "ok": data.get("ok"),
                    "cause": data.get("cause"),
                    "causes": list(data.get("causes") or []),
                    "summary": data.get("summary"),
                    "duration_s": data.get("duration_s"),
                    "skipped": (data.get("report") or {}).get("skipped"),
                    "route": record or None,
                })
                if record and not record.get("ok"):
                    _log(run, f"{NODE_INFO.get(node, (node,''))[0]}：{record.get('reason')}",
                         "warn")
                for nt in (upd.get("notes") or []):
                    _log(run, nt, "note")

                nxt = _next_of(node, upd, record, acc, pending_before)
                # 累积一份"我自己的状态视图"。节点返回的 update 就是它写进状态的东西，
                # 照 reducers 合并即可 —— 这样"下一步去哪"不依赖 LangGraph 内部读数。
                _merge(acc, upd)
                if "pending_approval" in upd:
                    pending_before = dict(upd.get("pending_approval") or {})
                if nxt:
                    _put(run, {"type": "node_start", "node": nxt, **_node_row(nxt)})

        snap = app.get_state(cfg)
        values = (snap.values if snap else {}) or {}
        interrupts = [_interrupt_value(i) for i in (getattr(snap, "interrupts", ()) or ())]

        run.halted = bool(values.get("halts"))
        run.waiting = bool(interrupts)

        _put(run, {
            "type": "finished",
            "halted": run.halted,
            "waiting": run.waiting,
            "next": list(getattr(snap, "next", ()) or ()),
            "brief": _brief(values),
            "final": values.get("final") or {},
            "approvals": dict(values.get("approvals") or {}),
            "artifacts": _artifacts(config, values),
        })
        if run.waiting:
            _log(run, "流程已暂停，状态已保存 —— 等你拍板", "human")
        elif run.halted:
            _log(run, "已停下（需人工介入）", "warn")
        else:
            _log(run, "走到终点", "ok")

    except Exception as e:  # noqa: BLE001 —— 后台线程的异常必须变成事件，否则前端只会干等
        run.error = f"{type(e).__name__}: {e}"
        import traceback
        _log(run, f"运行出错：{run.error}", "error")
        _put(run, {"type": "error", "message": run.error,
                   "traceback": traceback.format_exc()[-3000:]})
    finally:
        run.finished = True
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        _put(run, None)          # 哨兵：告诉 SSE 可以收尾了
        try:
            _BUSY.release()
        except RuntimeError:
            pass


def _artifacts(config: AppConfig, values: dict) -> list[dict]:
    """把成片链路上的产物转成**相对路径**清单，供前端按需取用。

    为什么用相对路径：绝对路径里有用户名与中文目录，塞进 URL 既难看又容易编码出错；
    后端的 `/api/artifact` 会把它重新约束回 `projects_root` 之内（越界直接 400）。
    """
    root = Path(config.projects_root).resolve()
    out: list[dict] = []

    def add(label: str, p: Path | str | None, kind: str) -> None:
        if not p:
            return
        path = Path(p)
        if not path.is_file():
            return
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            return
        out.append({"label": label, "rel": rel, "kind": kind,
                    "size": path.stat().st_size})

    project = Path(values.get("project") or "") if values.get("project") else None
    final = values.get("final") or {}
    add("成片 final.mp4", final.get("final_video")
        or (project / "renders" / "final.mp4" if project else None), "video")
    add("无特效版 assembled.mp4",
        final.get("assembled_backup")
        or (project / "renders" / "assembled.mp4" if project else None), "video")
    add("烧字全片 subtitled_full.mp4",
        project / "renders" / "subtitled_full.mp4" if project else None, "video")
    add("字幕 final.srt", project / "edit" / "subtitles" / "final.srt" if project else None,
        "text")
    add("剪后时间轴 timeline_map.json",
        project / "edit" / "timeline_map.json" if project else None, "json")
    add("剪前逐字稿 transcript.json",
        project / "edit" / "transcript.json" if project else None, "json")
    add("流水线报告 pipeline_report.json",
        project / "edit" / "pipeline_report.json" if project else None, "json")
    add("动效工程 index.html",
        project / "overlays" / "index.html" if project else None, "text")
    return out


# ============================================================================
#  HTTP 层
# ============================================================================
@asynccontextmanager
async def _lifespan(_app):
    cfg = _config()
    if cfg.database_url:
        from ..storage import check_database

        check_database(cfg)
    yield
    for c in list(_CONVS.values()):
        if c.conn is not None:
            c.conn.close()
            c.conn = None
            c.director = None


app = FastAPI(title="口播剪辑编排台", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.get("/api/health")
def health():
    cfg = _config()
    if cfg.database_url:
        from ..storage import check_database

        try:
            check_database(cfg)
        except Exception:
            raise HTTPException(503, "Database unavailable or migration required") from None
    return {"status": "ok", "storage": cfg.storage_backend}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta")
def meta():
    cfg = _config()
    key, key_src = resolve_api_key()
    problems = cfg.validate(require_model=False)
    if not key:
        problems.append("找不到模型网关密钥：设 LK_API_KEY，或把密钥放在桌面那份元气调用文档里")
    return {
        "chain": [_node_row(n, k) for n, k in DISPLAY_CHAIN],
        "owners": [S.DIRECTOR, S.CUT_PLANNER, S.SUBTITLE_MAKER,
                   S.OVERLAY_DESIGNER, S.QC_INSPECTOR],
        "scenarios": RH.scenario_list(),
        "problems": problems,
        "key_source": key_src,
        "key_present": bool(key),
        "projects_root": str(cfg.projects_root),
        "disk": {
            "path": str(Path(cfg.projects_root)),
            "free": _free_bytes(Path(cfg.projects_root)),
            "free_human": _fmt_size(_free_bytes(Path(cfg.projects_root))),
            "run_headroom_human": _fmt_size(RUN_HEADROOM_BYTES),
            "enough_for_upload": _free_bytes(Path(cfg.projects_root)) >= MIN_FREE_BYTES,
            "video_exts": sorted(VIDEO_EXTS),
        },
        "model": {"text": cfg.model.text_model, "vision": cfg.model.vision_model,
                  "base_url": cfg.model.base_url},
        "budgets": {"retake_budget": cfg.budgets.retake_budget,
                    "cross_agent_loop_budget": cfg.budgets.cross_agent_loop_budget,
                    "same_cause_limit": cfg.budgets.same_cause_limit,
                    "bgm_max_price": cfg.budgets.bgm_max_price},
        "modes": [
            {"key": "rehearsal", "title": "演练模式",
             "blurb": "桩内核 + 桩判断节点，跑同一张图。不花钱、不跑 ffmpeg、秒级。"
                      "⛔ 不产生真实产物。"},
            {"key": "real", "title": "真实运行",
             "blurb": "真内核 + 真模型。会调 GPU 转写与 ffmpeg 编码；"
                      "花钱的动作（BGM）必然停下来等你拍板。"},
        ],
    }


@app.get("/api/projects")
def projects():
    cfg = _config()
    root = Path(cfg.projects_root)
    if not root.is_dir():
        return {"projects": [], "error": f"projects_root 不存在：{root}"}

    from ..kernel import KernelClient
    client = KernelClient(cfg)
    out = []
    for d in sorted(root.iterdir()):
        # `_rehearsal` 是演练脚手架、`_incoming` 是上传暂存区 —— 都不是"项目"
        if not d.is_dir() or d.name in (RH.REHEARSAL_DIR, _INCOMING):
            continue
        row: dict = {"name": d.name, "path": str(d)}
        raw = d / "input" / "raw.mp4"
        row["has_raw"] = raw.is_file()
        if row["has_raw"]:
            row["raw_size"] = raw.stat().st_size
        try:
            r = client.status(d)
            rep = r.report or {}
            row["kernel_ok"] = r.ok
            row["next_stage"] = rep.get("next_stage")
            row["last_qc_ok"] = rep.get("last_qc_ok")
            row["segment_count"] = rep.get("segment_count")
            row["final_exists"] = bool((rep.get("artifacts") or {})
                                       .get("final_video", {}).get("exists"))
            row["resume_node"] = S.resume_node(rep.get("next_stage"))
            if not r.ok:
                row["kernel_cause"] = r.cause
        except Exception as e:  # noqa: BLE001 —— 一个项目探不动不该让整个列表挂掉
            row["kernel_ok"] = False
            row["kernel_cause"] = f"{type(e).__name__}: {e}"
        out.append(row)
    return {"projects": out, "projects_root": str(root)}


@app.post("/api/upload")
async def upload_source(file: UploadFile = File(...), name: str = Form("")):
    """传一个源视频进来 ⇒ 建一个新项目，落成 `input/raw.mp4`。

    这条路径上有三个"不可逆/昂贵"的点，都做了闸门：

      1. **磁盘**。一次完整跑实测占几百 MB~1 GB（现有项目 `_real_person` 262 MB、
         `_tts_lecture` 499 MB）。所以要求「剩余空间 ≥ 上传体积 + 2 GB 余量」才收，
         否则跑到一半 C 盘满 —— 那时 ffmpeg 报的错会指向一个完全无关的地方。
      2. **原子落盘**。先写 `raw.mp4.part` 再 `os.replace`。半截的 `raw.mp4`
         比没有更糟：`kb_status` 会认为"源片在了"，后面的阶段全在一个坏文件上跑。
      3. **先验后收**。落地前用 ffprobe 确认它真是**带音轨的视频** ——
         口播剪辑的整条链都建立在音频上（转写靠它、混音靠它）。没有音轨的文件
         要在这一步就说清楚，而不是等 stage1 抛一个看不懂的错。

    ⛔ 不覆盖已有项目：重名自动加后缀（见 `unique_project_dir`）。
    """
    cfg = _config()
    root = Path(cfg.projects_root)
    root.mkdir(parents=True, exist_ok=True)

    orig = Path(file.filename or "raw.mp4").name
    ext = Path(orig).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, f"不支持的格式 {ext or '(无扩展名)'}；"
                                 f"支持 {sorted(VIDEO_EXTS)}")

    declared = int(file.size or 0)
    free = _free_bytes(root)
    if free < MIN_FREE_BYTES:
        raise HTTPException(507, f"磁盘空间不足：{root} 所在盘只剩 {_fmt_size(free)}。"
                                 f"一次完整跑要几百 MB~1 GB，请先清理。")
    need = declared + RUN_HEADROOM_BYTES
    if free < need:
        raise HTTPException(507, f"磁盘空间不足：只剩 {_fmt_size(free)}，"
                                 f"这次要留 {_fmt_size(need)}"
                                 f"（上传 {_fmt_size(declared)} + 跑完所需余量 "
                                 f"{_fmt_size(RUN_HEADROOM_BYTES)}）。")

    # ---- 先落到暂存区，验过了再建项目 --------------------------------------
    staging = root / _INCOMING / uuid.uuid4().hex[:12]
    staging.mkdir(parents=True, exist_ok=True)
    part = staging / "raw.mp4.part"
    written = 0
    try:
        with part.open("wb") as f:
            while True:
                chunk = await file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                f.write(chunk)
                # 边写边查：客户端可能谎报 size（或者根本没有 Content-Length）
                if written + RUN_HEADROOM_BYTES > _free_bytes(root):
                    raise HTTPException(507, "写到一半发现磁盘不够了，已中止"
                                             "（上传的文件已丢弃）。")
    except HTTPException:
        _discard_tree(staging)
        raise
    except Exception as e:  # noqa: BLE001
        _discard_tree(staging)
        raise HTTPException(500, f"写入失败：{type(e).__name__}: {e}") from None

    if written == 0:
        _discard_tree(staging)
        raise HTTPException(400, "收到的是空文件。")

    from ..nodes.base import probe_video
    spec = probe_video(part)
    if spec.get("error") or not spec.get("has_video"):
        _discard_tree(staging)
        raise HTTPException(400, f"这不是一个能解码的视频（ffprobe："
                                 f"{spec.get('error') or '没有视频流'}）。")
    if not spec.get("has_audio"):
        _discard_tree(staging)
        raise HTTPException(400, "这个视频**没有音轨**。口播剪辑整条链都建立在音频上"
                                 "（转写、混音都靠它），请换一个带声音的源片。")

    uploaded = staging / "raw.mp4"
    os.replace(part, uploaded)

    # ---- 验过了，这才建项目 ------------------------------------------------
    pdir = unique_project_dir(root, sanitize_project_name(name, Path(orig).stem))
    for sub in ("input", "edit", "renders"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    os.replace(uploaded, pdir / "input" / "raw.mp4")      # 同盘 rename，原子
    _discard_tree(staging)                                 # 此时目录里已没有文件

    return {
        "project": pdir.name,
        "path": str(pdir),
        "renamed_from": orig if orig != "raw.mp4" else None,
        "bytes": written,
        "spec": spec,
        "free_after": _free_bytes(root),
        "note": "已建好项目。切到「真实运行」选中它即可开跑；付费动作仍会停下来问你。",
    }


def _discard_tree(staging: Path) -> None:
    """清掉本次上传的暂存目录。

    ⚠️ 顺序不能反：**先删文件、再 rmdir**。`rmdir` 在目录非空时会失败，
    而失败的表现恰好是"看起来清理过了、其实那个 `.part` 还占着几百 MB" ——
    一个被拒的上传会安静地留在盘上。这正是本文件别处反复提到的"静默失效"，
    所以这里按"先文件后目录"写死，且每一步都只看得到自己的东西。

    只动**自己刚建的那个**目录；`_incoming` 只在空了之后才删（非空时留着，
    当做一道保险：万一里面还有别的上传在跑，不误删）。
    """
    for name in ("raw.mp4.part", "raw.mp4"):
        try:
            (staging / name).unlink(missing_ok=True)
        except OSError:
            pass
    for p in (staging, staging.parent):
        try:
            p.rmdir()
        except OSError:
            pass


# ============================================================================
#  对话（**会话是一等公民**）
# ============================================================================
# 为什么不再是"一个项目一个会话、纯内存"：
#   · 用户要的是"打开就能说话" —— 不能让"先在下拉框里选个项目"成为开口的前提；
#   · 会话要能留得住（刷新页面、重启服务都还在），这是聊天界面的基本预期；
#   · 一个会话应当能中途换项目（用户传了新视频，或者说"看另一个项目"）。
# 会话包含标识、可选的项目绑定、持久化事件和独立检查点中的模型记忆。
MAX_EVENTS = 600           # 单个会话保留的事件条数（超了从头部丢）
MAX_CONVERSATIONS = 60     # 会话总数上限（超了先丢最久没动且没消息的）


@dataclass
class Conversation:
    id: str
    title: str = "新对话"
    project: str | None = None            # 绑定的项目名；None = 还没绑，照样能聊
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)   # 事件流水（刷新/重启后回看）

    # ---- 以下几项只在内存里，**不落盘** ----
    director: Any = None
    conn: Any = None                      # 会话专属检查点连接
    # ⛔ 付费授权位：**只能由 /api/chat/authorize 写入**。
    #    主管的任何工具都碰不到它 —— 闸门不能押在会自己说话的东西上（铁律 11）。
    #    刻意**不落盘**：授权是"这一次"的事，不该在重启后还活着。
    paid_ok: bool = False
    busy: bool = False

    def blob(self) -> dict:
        """落盘用的形态（**不带** director/连接/授权位）。"""
        return {"id": self.id, "title": self.title, "project": self.project,
                "created": self.created, "updated": self.updated,
                "events": self.events}


_CONVS: dict[str, Conversation] = {}
_CONVS_LOCK = threading.Lock()
_PERSIST_LOCK = threading.Lock()
_CONVS_LOADED = False
_TURN_OWNER: dict[str, str] = {}          # turn_id → conversation id（流式端点靠它定位）


def _persist(cfg: AppConfig) -> None:
    """持久化标题、绑定和事件；模型上下文由独立检查点存储管理。"""
    from ..storage import save_conversations

    with _PERSIST_LOCK:
        with _CONVS_LOCK:
            out = {"version": 1, "conversations": [
                c.blob() for c in sorted(_CONVS.values(), key=lambda c: c.updated, reverse=True)
            ]}
        save_conversations(cfg, out)


def _load_conversations(cfg: AppConfig) -> None:
    global _CONVS_LOADED
    with _CONVS_LOCK:
        if _CONVS_LOADED:
            return
        from ..storage import load_conversations

        data = load_conversations(cfg)
        for row in (data.get("conversations") or []):
            cid = str(row.get("id") or "")
            if not cid or cid in _CONVS:
                continue
            _CONVS[cid] = Conversation(
                id=cid, title=str(row.get("title") or "新对话"),
                project=(row.get("project") or None),
                created=float(row.get("created") or time.time()),
                updated=float(row.get("updated") or time.time()),
                events=list(row.get("events") or []),
            )
        _CONVS_LOADED = True


def _ensure_director(cfg: AppConfig, c: Conversation) -> Any:
    """给会话备一个主管（首次用时才建）。**同一个会话的多轮记忆靠它 + checkpointer。**"""
    if c.director is not None:
        return c.director
    from ..agent.director import make_director
    from ..graph import make_checkpointer

    pdir: Path | None = None
    if c.project:
        try:
            pdir = resolve_project(cfg, c.project)
            if not pdir.is_dir():
                pdir = None
        except (HTTPException, ValueError):
            pdir = None
        if pdir is None:               # 项目被删了就解绑，别让会话卡在一个不存在的对象上
            c.project = None

    c.conn = open_checkpoint_db(cfg, purpose="chat")
    try:
        c.director = make_director(cfg, pdir, checkpointer=make_checkpointer(c.conn))
    except Exception:
        c.conn.close()
        c.conn = None
        raise
    c.director.ctx.paid_ok = c.paid_ok
    return c.director


def _conv(cfg: AppConfig, cid: str) -> Conversation:
    _load_conversations(cfg)
    c = _CONVS.get(str(cid or ""))
    if c is None:
        raise HTTPException(404, "没有这个会话（可能已被删除，新建一个即可）")
    return c


def _new_conversation(cfg: AppConfig, project: str | None = None,
                      title: str | None = None) -> Conversation:
    _load_conversations(cfg)
    pname = None
    if project:
        pdir = resolve_project(cfg, project)
        if not pdir.is_dir():
            raise HTTPException(400, f"项目不存在：{pdir}")
        pname = pdir.name
    cid = uuid.uuid4().hex[:12]
    c = Conversation(id=cid, project=pname, title=(title or (pname or "新对话")))
    with _CONVS_LOCK:
        _CONVS[cid] = c
        _prune_conversations(cfg)
    _persist(cfg)
    return c


def _prune_conversations(cfg: AppConfig) -> None:
    """会话太多时丢最久没动、且**一句都没说过**的。有内容的会话不自动删。"""
    if len(_CONVS) <= MAX_CONVERSATIONS:
        return
    idle = sorted((c for c in _CONVS.values() if not c.events),
                  key=lambda c: c.updated)
    for c in idle[:len(_CONVS) - MAX_CONVERSATIONS]:
        _drop_conversation(c.id, cfg)


def _delete_memory(c: Conversation, cfg: AppConfig) -> None:
    """删除持久化记忆；重启后未创建主管的会话也需要清理。"""
    conn = c.conn or open_checkpoint_db(cfg, purpose="chat")
    try:
        make_checkpointer(conn).delete_thread(c.id)
    finally:
        if c.conn is None:
            conn.close()


def _drop_conversation(cid: str, cfg: AppConfig) -> None:
    c = _CONVS.get(cid)
    if c is None:
        return
    if c.busy:
        raise HTTPException(409, "会话正在运行，暂时不能删除。")
    _delete_memory(c, cfg)
    if c.conn is not None:
        c.conn.close()
    _CONVS.pop(cid, None)


def _note(c: Conversation, text: str) -> None:
    """往会话里插一条**界面可见、但不属于任何一轮**的说明。"""
    c.events.append({"type": "note", "text": text, "ts": time.time()})
    c.updated = time.time()


def _touch_events(c: Conversation) -> None:
    if len(c.events) > MAX_EVENTS:
        del c.events[:len(c.events) - MAX_EVENTS]


def _conv_summary(c: Conversation) -> dict:
    last = next((e for e in reversed(c.events)
                 if e.get("type") in ("assistant", "turn_start")), None)
    return {
        "id": c.id, "title": c.title, "project": c.project,
        "created": c.created, "updated": c.updated, "busy": c.busy,
        "messages": sum(1 for e in c.events if e.get("type") == "turn_start"),
        "last": (last or {}).get("text") or (last or {}).get("message") or "",
    }


@app.get("/api/conversations")
def conversations():
    """所有会话（新的在前）。**这个接口不需要项目、也不需要密钥。**"""
    cfg = _config()
    _load_conversations(cfg)
    with _CONVS_LOCK:
        rows = sorted(_CONVS.values(), key=lambda c: c.updated, reverse=True)
        return {"conversations": [_conv_summary(c) for c in rows]}


@app.post("/api/conversations")
async def conversation_create(request: Request):
    """新建一个会话。`project` 可以不给 —— 不给就是"还没绑定项目"，照样能说话。"""
    body = await request.json() if await request.body() else {}
    cfg = _config()
    c = _new_conversation(cfg, body.get("project"), body.get("title"))
    return _conv_summary(c)


@app.delete("/api/conversations/{cid}")
def conversation_delete(cid: str):
    """删会话（**连同它的模型记忆**）。项目里的产物一个都不动。"""
    cfg = _config()
    _load_conversations(cfg)
    if cid not in _CONVS:
        raise HTTPException(404, "没有这个会话")
    _drop_conversation(cid, cfg)
    _persist(cfg)
    return {"deleted": cid}


@app.post("/api/conversations/{cid}/bind")
async def conversation_bind(cid: str, request: Request):
    """把会话绑到某个项目上（`project` 传空字符串 = 解绑）。

    上传视频后前端会调它，好让主管**知道现在说的是哪个项目**。
    """
    body = await request.json()
    cfg = _config()
    c = _conv(cfg, cid)
    raw = body.get("project")
    if raw:
        pdir = resolve_project(cfg, str(raw))
        if not pdir.is_dir():
            raise HTTPException(400, f"项目不存在：{pdir}")
        c.project = pdir.name
    else:
        c.project = None
    if c.director is not None:                     # 立刻生效，不必重建主管
        c.director.ctx.project = (
            cfg.resolve_project_dir(Path(cfg.projects_root) / c.project)
            if c.project else None)
    note = str(body.get("note") or "").strip()
    if note:
        _note(c, note)
    c.updated = time.time()
    _persist(cfg)
    return {"id": c.id, "project": c.project}


@app.get("/api/conversations/{cid}/events")
def conversation_events(cid: str):
    """这个会话到目前为止的完整事件流水（刷新/重启后接着看）。"""
    cfg = _config()
    c = _conv(cfg, cid)
    return {"id": c.id, "title": c.title, "project": c.project,
            "paid_ok": c.paid_ok, "busy": c.busy, "events": c.events}


@app.post("/api/conversations/{cid}/clear")
async def conversation_clear(cid: str):
    """清空这个会话的对话（**连主管的多轮记忆一起清**）。项目产物一个都不动。"""
    cfg = _config()
    c = _conv(cfg, cid)
    if c.busy:
        raise HTTPException(409, "这个会话正在跑，等它说完再清。")
    # 记忆清掉的做法就是"换一个主管"（同一个 thread_id 上的历史留在库里，
    # 所以必须显式删掉那个 thread，否则新主管会读回旧上下文）。
    _delete_memory(c, cfg)
    if c.conn is not None:
        c.conn.close()
        c.conn = None
    c.events.clear()
    c.paid_ok = False
    c.director = None
    c.updated = time.time()
    _persist(cfg)
    return {"cleared": c.id}


# ============================================================================
#  一轮对话
# ============================================================================
@app.post("/api/chat")
async def chat(request: Request):
    """发一句话。返回 turn_id，事件用 `/api/chat/stream/{turn_id}` 取。"""
    body = await request.json()
    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "说的话不能是空的")
    cfg = _config()

    cid = str(body.get("conversation_id") or "").strip()
    if cid:
        c = _conv(cfg, cid)
    else:                                  # 没给会话就现开一个 —— 让"直接说话"始终成立
        c = _new_conversation(cfg, body.get("project"))

    problems = cfg.validate(require_model=True)
    if problems:
        raise HTTPException(400, "启动前自检不通过：" + "；".join(problems))
    if c.busy:
        raise HTTPException(409, "这个会话还有一轮在跑（工具可能正在转写/渲染）。"
                                 "等它说完再讲下一句。")

    _ensure_director(cfg, c)
    turn_id = uuid.uuid4().hex[:12]
    c.busy = True

    # 第一句话拿来当会话标题（比"新对话 1/2/3"有用得多）
    if not c.events:
        c.title = message[:18] + ("…" if len(message) > 18 else "")
    c.events.append({"type": "user", "text": message,
                     "turn_id": turn_id, "ts": time.time()})
    c.updated = time.time()
    _TURN_OWNER[turn_id] = c.id
    if len(_TURN_OWNER) > 400:
        for k in list(_TURN_OWNER)[:100]:
            _TURN_OWNER.pop(k, None)

    q: "queue.Queue[dict | None]" = queue.Queue()

    def worker() -> None:
        from ..agent.director import stream_turn

        try:
            for ev in stream_turn(c.director, c.id, message):
                ev["turn_id"] = turn_id
                ev.setdefault("ts", time.time())
                c.events.append(ev)
                q.put(ev)
                # 主管若把活交给"标准全流程"，前端据此去调 /api/run
                if (ev.get("type") == "tool_result"
                        and ev.get("name") == "run_standard_pipeline"
                        and "交给界面执行" in (ev.get("content") or "")):
                    q.put({"type": "suggest_standard_run", "turn_id": turn_id})
        finally:
            c.busy = False
            c.updated = time.time()
            # 主管可能用 use_project 换了项目（或自己绑了一个）—— 同步回来并落盘，
            # 否则界面上显示的还是旧项目，下次启动也对不上。
            try:
                p = c.director.ctx.project
                newname = Path(p).name if p is not None else None
                if newname != c.project:
                    c.project = newname
            except Exception:  # noqa: BLE001
                pass
            _touch_events(c)
            try:
                _persist(cfg)
            except Exception:
                q.put({"type": "error", "message": "会话记录保存失败，请检查存储服务。"})
            finally:
                q.put(None)

    threading.Thread(target=worker, daemon=True, name=f"chat-{turn_id}").start()
    return {"turn_id": turn_id, "conversation_id": c.id,
            "title": c.title, "project": c.project, "paid_ok": c.paid_ok}


@app.get("/api/chat/stream/{turn_id}")
def chat_stream(turn_id: str):
    """把这一轮的事件流出去（先补发已发生的，再跟着新的）。"""

    def gen():
        cfg = _config()
        cid = _TURN_OWNER.get(turn_id)
        if not cid:
            yield f"data: {json.dumps({'type': 'error', 'message': '这一轮不在本进程里了（服务重启过）——重发一次即可。'}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"
            return
        try:
            c = _conv(cfg, cid)
        except HTTPException:
            yield "event: done\ndata: {}\n\n"
            return
        idx = 0
        while True:
            while idx < len(c.events):
                ev = c.events[idx]
                idx += 1
                if ev.get("turn_id") != turn_id:
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            if not c.busy:
                break
            time.sleep(0.2)
        yield f"data: {json.dumps({'type': 'turn_done', 'turn_id': turn_id, 'conversation_id': c.id, 'project': c.project}, ensure_ascii=False)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.post("/api/chat/authorize")
async def chat_authorize(request: Request):
    """⛔ 唯一能打开付费闸门的地方 —— 由人在界面上点，agent 没有任何途径调用它。

    `ok=false` 用来在用户改变主意时把授权收回去。
    **授权一次一用**：`generate_bgm` 真提交成功后会立刻把它收回，防重复扣费。
    """
    body = await request.json()
    cfg = _config()
    c = _conv(cfg, str(body.get("conversation_id") or ""))
    c.paid_ok = bool(body.get("ok", True))
    if c.director is not None:
        c.director.ctx.paid_ok = c.paid_ok
    return {"conversation_id": c.id, "paid_ok": c.paid_ok}


@app.get("/api/state")
def state(project: str, thread: str | None = None):
    cfg = _config()
    try:
        pdir = resolve_project(cfg, project)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    tid = thread or pdir.name
    return read_state(cfg, pdir, tid)


@app.post("/api/run")
async def start_run(request: Request):
    body = await request.json()
    cfg = _config()

    mode = str(body.get("mode") or "rehearsal")
    if mode not in ("real", "rehearsal"):
        raise HTTPException(400, f"未知模式 {mode!r}")

    approve = body.get("approve", None)
    project = str(body.get("project") or "").strip()
    thread = (str(body.get("thread") or "").strip() or None)

    # ---- 校验：早失败比跑一半失败好（与 run.py 同一口径）-------------------
    if approve is not None and not isinstance(approve, bool):
        raise HTTPException(400, "approve 只能是 true / false")

    if mode == "real":
        key, _ = resolve_api_key()
        problems = cfg.validate(require_model=True)
        if not key:
            problems.append("找不到模型网关密钥（LK_API_KEY / YUANQI_API_KEY）")
        if problems:
            raise HTTPException(400, "启动前自检不通过：" + "；".join(problems))
        if not project:
            raise HTTPException(400, "真实运行必须指定项目目录")
        try:
            pdir = resolve_project(cfg, project)
        except ValueError as e:
            raise HTTPException(400, f"项目路径不合法：{e}") from None
        if not pdir.is_dir():
            raise HTTPException(400, f"项目目录不存在：{pdir}")
        scenario = None
    else:
        scenario = str(body.get("scenario") or "happy")
        if scenario not in RH.SCENARIOS:
            raise HTTPException(400, f"未知剧本 {scenario!r}")
        pdir = None        # 演练的目录在下面按 nonce 新建

    # ---- 只允许一个运行在跑 -------------------------------------------------
    if not _BUSY.acquire(blocking=False):
        raise HTTPException(409, "已有一个运行在进行中。本应用的状态在同一个检查点库里，"
                                 "并发跑两路会互相踩 —— 等它结束，或先点「停止观察」。")

    try:
        nonce = uuid.uuid4().hex[:8]

        if approve is not None:
            # ---- 恢复：**必须**回到原来那个 thread 与项目目录 ----------------
            # 演练的恢复也要走这里：它的项目目录带 nonce，凭 scenario 是找不回来的。
            if not thread:
                raise HTTPException(400, "恢复执行必须带 thread（界面会自动带上）。")
            if mode == "real":
                input_kind = "resume"
            else:
                recorded = _THREAD_DIR.get(thread)
                if not recorded:
                    raise HTTPException(
                        400, f"找不到 thread={thread} 对应的项目目录 —— "
                             "可能服务重启过。重新发起一次演练即可。")
                pdir = Path(recorded)
                input_kind = "resume"
            if mode == "real":
                snap = read_state(cfg, pdir, thread)
                if not snap["interrupts"]:
                    raise HTTPException(400, "当前没有挂起的事项要拍板 —— "
                                             "无需批准/拒绝（这条闸门不接受空放行）")
            thread_id = thread
        elif mode == "rehearsal":
            try:
                RH.prune_old_projects(cfg)
            except Exception:  # noqa: BLE001 —— 清理失败绝不能把演练搞挂
                pass
            pdir = RH.fresh_project(cfg, scenario)
            thread_id = thread or f"rh:{scenario}:{nonce}"
            input_kind = "start"
        else:
            thread_id = thread or pdir.name
            input_kind = "start"

        if mode == "rehearsal":
            # 演练的 goal 固定 —— 让六个剧本每次都从同一状态出发，才有可比性
            goal = {"overlay_required": True, "bgm_source": "generate",
                    "allow_paid": False, "allow_expensive": False}
        else:
            goal = {
                "overlay_required": bool(body.get("overlay_required", True)),
                "bgm_source": str(body.get("bgm_source") or "generate"),
                # 预授权默认**关**：这张卡存在的意义就是让付费动作先停下来问人
                "allow_paid": bool(body.get("allow_paid", False)),
                "allow_expensive": bool(body.get("allow_expensive", False)),
            }
            ov = body.get("override") or {}
            action = str(ov.get("action") or "").strip()
            if action:
                reason = str(ov.get("reason") or "")
                if len(reason.strip()) < 10:
                    raise HTTPException(400, "例外放行的理由要 ≥10 字 —— "
                                             "例外必须是一次有记录的决定")
                goal["override"] = {"action": action, "reason": reason}

        _remember_thread(thread_id, pdir)
        run = Run(id=nonce, mode=mode, scenario=scenario, project_dir=str(pdir),
                  thread_id=thread_id, input_kind=input_kind, approve=approve, goal=goal)
        with _RUNS_LOCK:
            _RUNS[run.id] = run
            _RUN_ORDER.append(run.id)
            while len(_RUN_ORDER) > MAX_KEEP_RUNS:
                _RUNS.pop(_RUN_ORDER.pop(0), None)

        threading.Thread(target=_execute, args=(run, cfg), daemon=True,
                         name=f"run-{run.id}").start()
        return {"run_id": run.id, "thread_id": thread_id, "mode": mode,
                "scenario": scenario, "project": str(pdir), "input": input_kind}
    except Exception:
        _BUSY.release()
        raise


@app.get("/api/stream/{run_id}")
def stream(run_id: str):
    run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "没有这个运行（可能已被回收，重新发起即可）")

    def gen():
        # 先补发已经发生过的事件？不做 —— 前端在发起后立刻连接，不存在漏读窗口；
        # 加缓冲只会让"到底实时不实时"这件事变得难判断。
        while True:
            try:
                ev = run.q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


_MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mp3": "audio/mpeg",
         ".json": "application/json; charset=utf-8", ".srt": "text/plain; charset=utf-8",
         ".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


@app.get("/api/artifact")
def artifact(rel: str, request: Request):
    """取产物文件。**只能取 `projects_root` 之内的文件**（越界一律 400）。

    ⚠️ 支持 Range：成片 15 MB 起，不支持拖进度条的 `<video>` 基本没法用。
    Starlette `FileResponse` 对 Range 的支持随版本变化，这里自己实现对 206 的处理，
    行为确定；非 Range 请求仍交回 `FileResponse`。
    """
    cfg = _config()
    root = Path(cfg.projects_root).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(400, "路径越界：只能取 projects_root 之内的文件") from None
    if not target.is_file():
        raise HTTPException(404, f"文件不存在：{rel}")

    media = _MIME.get(target.suffix.lower(), "application/octet-stream")
    size = target.stat().st_size
    rng = request.headers.get("range")

    if not rng:
        return FileResponse(target, media_type=media,
                            headers={"Accept-Ranges": "bytes"})

    m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
    if not m:
        raise HTTPException(416, "Range 格式不支持")
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPException(416, f"Range 越界（文件 {size} 字节）")

    def body():
        with target.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = f.read(min(256 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(body(), status_code=206, media_type=media, headers={
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    })


@app.get("/api/runs")
def runs():
    with _RUNS_LOCK:
        return {"runs": [
            {"id": r.id, "mode": r.mode, "scenario": r.scenario,
             "project_dir": r.project_dir, "thread_id": r.thread_id,
             "input": r.input_kind, "finished": r.finished,
             "halted": r.halted, "waiting": r.waiting, "error": r.error,
             "events": r.events, "started_at": r.started_at}
            for r in (_RUNS[i] for i in _RUN_ORDER if i in _RUNS)
        ]}


@app.post("/api/busy/release")
def release_busy():
    """万一后台线程死了而锁没放（例如被强杀），给一条明确的手动出路。

    比"重启服务"友好，也比在锁上做超时猜测可靠。
    """
    if _BUSY.acquire(blocking=False):
        _BUSY.release()
        return {"released": False, "note": "当前并没有在跑的运行"}
    try:
        _BUSY.release()
    except RuntimeError:
        pass
    return {"released": True}


def main(host: str = "127.0.0.1", port: int = 8760) -> None:
    import uvicorn

    key, src = resolve_api_key()
    print(f"[编排台] 密钥来源：{src}{'' if key else '（真实运行会不可用；演练模式不受影响）'}")
    cfg = _config()
    print(f"[编排台] 项目根目录：{cfg.projects_root}")
    for p in cfg.validate(require_model=False):
        print(f"[编排台] ⚠️ {p}")
    print(f"[编排台] 打开 http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    main(host=(argv[0] if argv else "127.0.0.1"),
         port=(int(argv[1]) if len(argv) > 1 else 8760))
