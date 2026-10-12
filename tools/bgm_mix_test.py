"""BGM 混音与质检的端到端测试（回答「谁来负责 BGM」的可执行证据）。

为什么要有这个测试：
    ⛔ **质检员听不到声音**（DSH 只有 `read_image`，没有读音频的工具）。
    所以"BGM 有没有进去 / 有没有盖住人声 / 是不是硬切"这三件事**不能靠听**，
    必须由 `stage6_qc._bgm_checks()` 测成数字。这个测试就是证明那些数字真的会动。

做法：
    造一条"纯人声"视频（灰底 + 180Hz 正弦，-18dB）与一条"BGM"音轨（440Hz 正弦，-14dB），
    走真实的 `kb.py mix` → `kb.py qc --target final` 链路，逐个用例断言 cause。

用例：
    A 正常（bgm_volume=0.30）  → bgm 四项全过
    B 过响（bgm_volume=1.5）   → bgm_not_overpowering FAIL → bgm_overpowers_voice
    C 静音（bgm_volume=0.0）   → bgm_present FAIL → bgm_silent
    D 素材缺失                 → bgm_missing（回剪裁策划：调 kb_bgm 生成，或取回用户曲目）
    E 音乐铺不满 + 无淡出      → bgm_abrupt_cut
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.common import FFMPEG, PYTHON_EXE, Project, read_json, run  # noqa: E402

PROJ = ROOT / "projects" / "_selftest_bgm"
VIDEO_SEC = 8.0


def _tone_video(path: Path, seconds: float, freq: int, vol_db: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=gray:s=320x180:r=15",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
        "-t", str(seconds), "-af", f"volume={vol_db}dB",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ])


def _tone_audio(path: Path, seconds: float, freq: int, vol_db: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
        "-t", str(seconds), "-af", f"volume={vol_db}dB", str(path),
    ])


def _run_kb(*args: str) -> int:
    p = subprocess.run(
        [str(PYTHON_EXE), str(ROOT / "kb.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    return p.returncode


def _bgm_of(checks: list[dict]) -> dict[str, dict]:
    return {c["id"]: c for c in checks if c["id"].startswith("bgm_")}


def _case(project: Project, name: str, spec: dict) -> tuple[dict[str, dict], dict]:
    (project.edit_dir / "mix.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _run_kb("mix", "--project", str(project.root), "--spec", "edit/mix.json")
    _run_kb("qc", "--project", str(project.root), "--target", "final")
    report = read_json(project.qc_report)
    bc = _bgm_of(report.get("checks") or [])
    print(f"  [{name}] " + ("  ".join(
        f"{k}={'PASS' if v['ok'] else 'FAIL'}" for k, v in sorted(bc.items())) or "（无 bgm 检查项）"))
    return bc, report


def main() -> int:
    if PROJ.is_dir():
        shutil.rmtree(PROJ)
    project = Project.resolve(PROJ)
    project.ensure_dirs()

    print("=== 造素材 ===")
    voice = project.root / "input" / "voice.mp4"
    bgm8 = project.root / "input" / "bgm_8s.wav"
    bgm2 = project.root / "input" / "bgm_2s.wav"
    _tone_video(voice, VIDEO_SEC, 180, -18)
    _tone_audio(bgm8, VIDEO_SEC, 440, -14)
    _tone_audio(bgm2, 2.0, 440, -14)
    print(f"  voice={voice.is_file()} bgm8={bgm8.is_file()} bgm2={bgm2.is_file()}")

    base = {"video": "input/voice.mp4", "output": "renders/final.mp4"}
    fails = 0

    def expect(cond: bool, msg: str) -> None:
        nonlocal fails
        print(f"    {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            fails += 1

    print("\n=== A 正常混音（bgm_volume=0.30）===")
    a, rep_a = _case(project, "A", {**base, "bgm": "input/bgm_8s.wav", "bgm_volume": 0.30})
    expect(len(a) >= 4, f"bgm 检查项 ≥4（实得 {len(a)}）")
    expect(all(v["ok"] for v in a.values()),
           "四项全过 " + "; ".join(f"{k}:{v['detail']}" for k, v in a.items() if not v["ok"]))

    # ── 探针完整性守门 ──
    # 这一组是为了防住一类**最危险的 bug：断言静默失效**。
    # 曾经 ffmpeg 的滤波统计写在 stderr，而 `run()` 只返回 stdout ⇒
    # `audio_no_clipping` 根本没进检查列表、两个黑帧断言永远"通过"，
    # 质检报告看着全绿，其实那三项从未运行过。
    ids = {c["id"] for c in (rep_a.get("checks") or [])}
    print("\n=== 探针完整性（防断言静默失效）===")
    for cid in ("audio_no_clipping", "no_black_frame_head", "no_black_frame_tail"):
        expect(cid in ids, f"检查项 {cid} 真的出现在报告里")

    print("\n=== B 过响（bgm_volume=1.5）===")
    b, _ = _case(project, "B", {**base, "bgm": "input/bgm_8s.wav", "bgm_volume": 1.5})
    expect(b.get("bgm_not_overpowering", {}).get("ok") is False, "bgm_not_overpowering FAIL")
    expect(b.get("bgm_not_overpowering", {}).get("cause") == "bgm_overpowers_voice",
           f"cause=bgm_overpowers_voice（实得 {b.get('bgm_not_overpowering', {}).get('cause')}）")

    print("\n=== C 静音（bgm_volume=0.0）===")
    c, _ = _case(project, "C", {**base, "bgm": "input/bgm_8s.wav", "bgm_volume": 0.0})
    expect(c.get("bgm_present", {}).get("ok") is False, "bgm_present FAIL")
    expect(c.get("bgm_present", {}).get("cause") == "bgm_silent",
           f"cause=bgm_silent（实得 {c.get('bgm_present', {}).get('cause')}）")

    print("\n=== D 素材缺失 ===")
    d, _ = _case(project, "D", {**base, "bgm": "input/not_here.wav", "bgm_volume": 0.3})
    expect(d.get("bgm_source_exists", {}).get("ok") is False, "bgm_source_exists FAIL")
    expect(d.get("bgm_source_exists", {}).get("cause") == "bgm_missing",
           f"cause=bgm_missing（实得 {d.get('bgm_source_exists', {}).get('cause')}）")

    print("\n=== E 音乐铺不满 + 无淡出（显式关掉循环）===")
    e, _ = _case(project, "E", {**base, "bgm": "input/bgm_2s.wav",
                                "bgm_volume": 0.30, "bgm_fade_out": 0,
                                "bgm_loop": False})
    expect(e.get("bgm_covers_duration", {}).get("ok") is False, "bgm_covers_duration FAIL")
    expect(e.get("bgm_has_fade_out", {}).get("ok") is False, "bgm_has_fade_out FAIL")
    expect(e.get("bgm_covers_duration", {}).get("cause") == "bgm_abrupt_cut",
           "cause=bgm_abrupt_cut")

    print("\n=== E2 同素材但开循环（应通过 —— Suno 单曲最长 4 分钟，长片靠循环）===")
    e2, _ = _case(project, "E2", {**base, "bgm": "input/bgm_2s.wav",
                                  "bgm_volume": 0.30, "bgm_loop": True})
    expect(e2.get("bgm_covers_duration", {}).get("ok") is True,
           f"bgm_covers_duration PASS（{e2.get('bgm_covers_duration', {}).get('detail')}）")
    expect(e2.get("bgm_not_overpowering", {}).get("ok") is True,
           "开循环后 BGM 仍持续垫底、响度正常")

    print("\n" + "=" * 46)
    print(f"结果：{'全绿' if fails == 0 else str(fails) + ' 项失败'}")
    print(f"（测试工程保留在 {PROJ}，可复查；不需要时直接删目录）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
