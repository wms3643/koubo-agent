"""测试共用的桩件。

抽出来只有一个理由：`FakeClient` 与两个测试文件都用得到，**复制两份必然漂移**
（改了一处忘了另一处，然后其中一个测试开始测一个不存在的世界）。
"""

from __future__ import annotations

import types
from pathlib import Path

from langgraph_app.kernel import KernelResult


def OK(tool: str, **report) -> KernelResult:
    return KernelResult(tool=tool, ok=True, exit_code=0,
                        report={"ok": True, "stage": tool, **report})


def FAIL(tool: str, cause: str, **report) -> KernelResult:
    return KernelResult(tool=tool, ok=False, cause=cause, exit_code=2,
                        report={"ok": False, "cause": cause, **report})


class FakeClient:
    """按 `script` 出结果的假内核（键形如 `cut` / `burn:test` / `assemble:assemble`）。

    值是**结果队列**（每次调用弹一个；弹完就用默认的 OK）——
    这样"第一次失败、第二次成功"这类回退场景可以精确编排。
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


class FakeLLM:
    """假模型：按固定文本回复，并把收到的提示词记下来（供断言"反馈有没有被背回去"）。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append(flatten(messages))
        return types.SimpleNamespace(content=self.reply)


def flatten(messages) -> str:
    """把消息列表压成一段纯文本（含图片块占位，便于断言"图有没有带上"）。"""
    out = []
    for m in messages:
        content = getattr(m, "content", m)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    out.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "image_url":
                        out.append(f"<image:{block['image_url']['url'][:32]}>")
                    else:
                        out.append(str(block.get("text") or ""))
        else:
            out.append(str(content))
    return "\n".join(out)


def write_plan_context(project: Path, segments: list[dict]) -> None:
    """给剪裁策划造一份 `edit/plan_context.json`（内核会产出它，测试里手写）。"""
    from langgraph_app.nodes.base import write_json

    write_json(project / "edit" / "plan_context.json", {
        "segments": segments,
        "long_gaps": [],
        "repeat_clusters": [],
        "stats": {"total": len(segments)},
    })


def seg(idx: int, text: str, *, suspect: bool = False) -> dict:
    return {"idx": idx, "start": float(idx), "end": float(idx) + 1.0, "dur": 1.0,
            "chars": len(text), "text": text,
            "suspect_hallucination": suspect}
