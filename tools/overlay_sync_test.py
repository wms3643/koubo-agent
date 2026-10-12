"""动效层「叠加合成」端到端自检（零模型、零 GPU，只需 ffmpeg）。

验的是那条最容易被嘴上跳过的链路：**动效层渲染成透明 MOV 之后，真能叠到成片上吗？**

方法（不靠"看起来对"，靠像素）：
  1. 造 base.mp4 —— 灰底 + 440Hz 音，5s
  2. 造 overlays/ov.mov —— 左上角红块，**ProRes4444 带 alpha（0.7 半透明）**，5s
  3. 走真实 `kb.py assemble`，spec 里带 `overlays`
  4. 抽第 1s 的帧，比左上角第一个像素：
       base 应 ≈ (128,128,128) 灰
       final 应明显偏红（R 高、G/B 低）⇒ 证明 alpha 被正确处理、叠加真的生效
  5. 顺带验时长没被叠加改变、音频还在

⚠️ 为什么这值得单独测：`assemble` 原本只有 xfade **串联**能力，没有 overlay **叠加**能力 ——
   两者是完全不同的 ffmpeg 图。串联对了不代表叠加对。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.common import FFMPEG, FFPROBE, write_json  # noqa: E402

W, H, DUR = 320, 240, 5.0


def sh(cmd: list[str]) -> bytes:
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace")[-900:])
    return p.stdout or b""


def first_pixel(video: Path, t: float = 1.0) -> tuple[int, int, int]:
    """取 t 秒处第一帧的**左上角第一个像素**（RGB）。"""
    raw = sh([
        FFMPEG, "-v", "error", "-ss", str(t), "-i", str(video),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ])
    if len(raw) < 3:
        raise RuntimeError("抽帧为空")
    return raw[0], raw[1], raw[2]


def duration(video: Path) -> float:
    out = sh([FFPROBE, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(video)])
    return float(out.decode().strip())


def main() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   | {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    prj = ROOT / "projects" / "_synctest"
    for d in ("input", "edit", "renders", "overlays"):
        (prj / d).mkdir(parents=True, exist_ok=True)
    base = prj / "renders" / "base.mp4"
    ov = prj / "overlays" / "ov.mov"
    final = prj / "renders" / "final.mp4"

    print("—— 造素材 ——")
    sh([FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=gray:s={W}x{H}:r=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(DUR), "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(base)])
    check("base.mp4 生成", base.is_file(), f"{duration(base):.2f}s")

    sh([FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=red:s=100x80:r=30",
        "-t", str(DUR),
        "-vf", "format=yuva444p10le,colorchannelmixer=aa=0.7",
        "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
        str(ov)])
    check("透明 MOV 生成（ProRes4444）", ov.is_file(), f"{ov.stat().st_size // 1024} KB")

    b_px = first_pixel(base)
    check("base 左上角是灰", all(100 <= c <= 160 for c in b_px), str(b_px))

    print("—— 走真实 assemble（带 overlays）——")
    spec = prj / "edit" / "assemble.json"
    write_json(spec, {
        "segments": [{"path": "renders/base.mp4"}],
        "overlays": [{"path": "overlays/ov.mov"}],
        "output": "renders/final.mp4",
    })
    p = subprocess.run(
        [sys.executable, str(ROOT / "kb.py"), "assemble",
         "--project", str(prj), "--spec", str(spec)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    check("assemble 退出码 0", p.returncode == 0, (p.stderr or p.stdout or "")[-200:])
    check("final.mp4 生成", final.is_file())
    if not final.is_file():
        print("\n无法继续，final 未生成")
        return 1

    f_px = first_pixel(final)
    r, g, b = f_px
    check("叠加生效：左上角明显偏红", r > 170 and r > g + 80 and r > b + 80,
          f"base={b_px} -> final={f_px}")
    check("alpha 被正确混合（非纯红/非未叠加）",
          15 < g < 100 and 15 < b < 100 and abs(g - b) <= 20, f"G={g} B={b}")

    db, df = duration(base), duration(final)
    check("叠加不改变时长", abs(df - db) <= 0.2, f"{db:.2f}s -> {df:.2f}s")

    print(f"\n{'=' * 46}")
    print("全部通过" if not fails else f"{len(fails)} 项失败")
    for f in fails:
        print(f"  - {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
