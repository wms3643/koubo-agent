"""成片链路的规格落盘 —— **唯一实现**（真节点与端到端桩测试共用）。

为什么单独抽出来：`plan_final`（真节点）要写这三份规格，端到端桩测试也得写
（否则 `apply_overlay` 会因缺 `assemble.json` 而停）。各写一份必然漂移 ——
而漂移的后果恰好最糟：**桩测试全绿、真跑却接不上**（或反过来）。

⛔ 三份规格里的**文件路径全部由代码写死**，不取 LLM 输入。理由见 `stages.py`
里那段"成片链路上的固定产物路径"：真跑暴露过"成片永不产出"与"成片静默没有 BGM"
两个后果，两件都是**管道一致性**，不是专业判断。
"""

from __future__ import annotations

from pathlib import Path

from .. import stages as S
from .base import write_json

DEFAULT_VOLUME = 0.12


def write_final_specs(
    *,
    project: Path,
    style: str,
    mix_in: dict,
    total: float,
    max_price: float,
) -> dict:
    """写 `edit/bgm.json` + `edit/mix.json` + `edit/assemble.json`，返回报告摘要。

    · `bgm.json`      —— 风格与单价上限（给付费生成用）
    · `mix.json`      —— 混哪两个文件、音量分段、淡出、循环（判断来自 LLM，路径来自代码）
    · `assemble.json` —— **骨架这一步**的规格；`apply_overlay` 会在叠加动效前改写它
    """
    sections = mix_in.get("bgm_volume_sections") or [
        {"start": 0.0, "end": total or 9e3, "volume": DEFAULT_VOLUME}]

    write_json(project / "edit" / "bgm.json", {
        "style": style,
        "mv": "chirp-v4-5",
        "sample_rate": 48000,
        "bitrate": "192k",
        "output": "bgm/bgm.mp3",
        "max_price": float(max_price),
    })
    write_json(project / "edit" / "mix.json", {
        # 骨架是**单段缝接**（`-c copy`）⇒ 音频保留，可以当"纯人声主轨"喂给 mix
        "video": S.SKELETON_VIDEO,
        "bgm": "bgm/bgm.mp3",
        "output": S.MIXED_MASTER,
        "bgm_volume_sections": sections,
        "bgm_fade_in": float(mix_in.get("bgm_fade_in", 0.5)),
        "bgm_fade_out": float(mix_in.get("bgm_fade_out", 1.5)),
        "bgm_loop": bool(mix_in.get("bgm_loop", True)),
    })
    write_json(project / "edit" / "assemble.json", {
        "segments": [{"path": S.SUBTITLED_FULL, "kind": "main"}],
        "transition": 0.5,
        "output": S.SKELETON_VIDEO,
    })
    return {
        "bgm_style_chars": len(style),
        "volume_sections": len(sections),
        "skeleton_input": S.SUBTITLED_FULL,
        "mixed_master": S.MIXED_MASTER,
    }
