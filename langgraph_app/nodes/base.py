"""节点公共件 —— 统一"跑一件事 → 记账 → 决定去哪"的形状。

所有节点都长这样：**只做一件事**，然后把结果交给 `routing.advance()` 去记账与裁决。
节点里**不出现**任何 `if cause == "..."` 的分支 —— 那些判断全在 `routing.MATRIX` 里。
这条纪律的价值在改动发生时体现：新增一个 cause 只改矩阵一处，18 个节点一行都不用动。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from langgraph.types import Command

from .. import routing as R
from ..config import AppConfig
from ..kernel import KernelClient, KernelResult
from ..state import PipelineState

NodeFn = Callable[[PipelineState], Command]


# ---------------------------------------------------------------------------
#  输出 / 探测的小工具（刻意**不** import 内核的 pipeline 包）
# ---------------------------------------------------------------------------
def probe_video(path: Path) -> dict:
    """用 ffprobe 取规格，供交付报告使用。

    ⚠️ 为什么不复用内核的 `pipeline.common.probe()`：本应用对内核的依赖**只有 CLI 契约**。
    借用内核模块会把"编排层"和"确定性层"在 import 图上粘起来，于是内核一改重构
    （哪怕只是挪个函数）就会连带把编排层弄坏 —— 而那正是分层要避免的事。
    这里自己调 ffprobe，只有十来行，但边界是干净的。
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        data = json.loads(proc.stdout or "{}")
    except Exception as e:  # noqa: BLE001 —— 探测失败不该让交付报错
        return {"error": f"{type(e).__name__}: {e}"}

    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = 0.0
    rate = (v or {}).get("r_frame_rate") or ""
    if "/" in rate:
        num, den = rate.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else 0.0
    return {
        "duration": round(float(data.get("format", {}).get("duration", 0.0)), 3),
        "width": (v or {}).get("width"),
        "height": (v or {}).get("height"),
        "fps": fps,
        "video_codec": (v or {}).get("codec_name"),
        "audio_codec": (a or {}).get("codec_name"),
        "has_video": v is not None,
        "has_audio": a is not None,
    }


def write_json(path: Path, payload) -> None:
    """原子写（tmp + os.replace），避免中途中断留下半截 JSON。

    不直接覆盖目标文件：报告是给人和后续步骤读的，"半截文件"比"没有文件"更难查。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
#  节点骨架
# ---------------------------------------------------------------------------
def command(goto: str, update: dict) -> Command:
    """构造 `Command`。唯一的作用是把 `HUMAN` 翻成 `HALT`（映射只有一处）。"""
    return Command(goto=R.graph_target(goto), update=update)


def kernel_step(
    *, node: str, client: KernelClient, config: AppConfig,
    call: Callable[[], KernelResult], state: PipelineState,
) -> Command:
    """跑一次内核调用并记账 —— **所有确定性节点都走这一条路**。"""
    result = call()
    goto, update = R.advance(node=node, result=result, state=state, config=config)
    return command(goto, update)


def synthetic(
    node: str, *, ok: bool, cause: str | None = None, report: dict | None = None
) -> KernelResult:
    """构造一个"非内核来源"的结果（LLM 输出不合格 / 评审裁决 / 跳过）。

    为什么要借用 `KernelResult` 这个形状：这样它们与内核结果走**同一条**路由与记账路径。
    另起一套"LLM 结果类型"就得再写一遍判读、再写一遍记账 —— 而那正是漂移的温床。
    """
    return KernelResult(
        tool=node, ok=ok, cause=None if ok else cause,
        exit_code=0 if ok else 2,
        report=report if report is not None else {"ok": ok, "cause": cause},
    )


def skip(node: str, state: PipelineState, config: AppConfig, note_text: str) -> Command:
    """"这一步不做"（跳过 / 免费复用）也是一种结果 —— **必须照样记账**。

    ⚠️ 为什么这不是形式主义：早先这些分支直接 `return command(goto, {...})` 绕过了
    `routing.advance()`，后果是节点在 `results` / `route_history` 里**凭空消失**。
    而交付报告正是靠这两个账本回答"哪些阶段真的跑过" —— 于是它会得出
    "本片没执行 BGM"这种错误结论（实际执行了，只是免费复用了已有成品）。
    记账路径只有一条，跳过的分支也得走它。
    """
    result = synthetic(node, ok=True, report={"ok": True, "skipped": note_text})
    goto, update = R.advance(node=node, result=result, state=state, config=config)
    notes = list(update.get("notes") or [])
    notes.append(note_text)
    update["notes"] = notes
    return command(goto, update)


def project_of(state: PipelineState) -> Path:
    return Path(state["project"])


def revision_of(state: PipelineState, key: str, default=None):
    """评审给出的重做参数（例如字幕档脸时要调的 `margin_v`）。"""
    return (state.get("revision") or {}).get(key, default)


def note_list(text: str) -> list[str]:
    return [text]
