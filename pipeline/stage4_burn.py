"""阶段 4 — 字幕烧录（两段式：测试片 → 全片）

skill 红线：
    · **烧录前必出 10s 测试片**，抓帧确认位置/遮挡/对齐，⛔ 禁止"差不多直接烧全片"
    · 字幕样式 `force_style` 照抄，禁止自调；`MarginV` 是离底边距离，**越大越往上**
    · filter 内 Windows 绝对路径必须转义盘符冒号：`C\\:/Users/...`
    · 全片烧录音频 `-c:a copy` 免二次损失；抓帧图落 `edit/tmpimg/`，⛔ 勿用 `/tmp`

两个子命令：
    burn-test  只烧前 10s 并抓帧，返回帧路径给视觉子代理判断
    burn-full  烧全片，输出 renders/subtitled_full.mp4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import (  # noqa: E402
    FFMPEG, Project, emit, guarded, probe, run, subtitle_force_style,
)

TEST_SECONDS = 10
TEST_FRAMES_AT = (1.5, 4.0, 8.0)
BURN_CRF = 20
BURN_PRESET = "medium"


def _subtitles_filter(srt: Path, margin_v: float | None = None) -> str:
    """构造 libass 滤镜串：Windows 路径反斜杠改正斜杠、盘符冒号转义。

    `margin_v` 是 `subtitle_occlusion`（字幕挡脸）回退的**唯一**可调项，
    见 `pipeline/common.subtitle_force_style()` 的说明。
    """
    escaped = srt.resolve().as_posix().replace(":", "\\:")
    return (
        f"subtitles=filename='{escaped}':"
        f"force_style='{subtitle_force_style(margin_v)}'"
    )


def _burn(src: Path, dst: Path, vf: str, *, audio_copy: bool, duration: float | None) -> None:
    cmd = [FFMPEG, "-y", "-i", str(src)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += [
        "-vf", vf,
        "-c:v", "libx264", "-preset", BURN_PRESET, "-crf", str(BURN_CRF),
        "-pix_fmt", "yuv420p",
    ]
    cmd += ["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "192k"]
    cmd += [str(dst)]
    run(cmd)


def cmd_test(project: Project, *, margin_v: float | None = None) -> None:
    src, srt = project.edited_video, project.final_srt
    if not src.is_file():
        emit({"ok": False, "cause": "missing_input", "error": f"缺少 {src}"}, exit_code=2)
    if not srt.is_file():
        emit({"ok": False, "cause": "missing_srt", "error": f"缺少 {srt}"}, exit_code=2)

    project.tmpimg_dir.mkdir(parents=True, exist_ok=True)
    vf = _subtitles_filter(srt, margin_v)

    # 测试片最长 10s；不足 10s 就整段
    total = probe(src)["duration"]
    span = min(TEST_SECONDS, total)
    _burn(src, project.subtitle_test_video, vf, audio_copy=True, duration=span)

    # ⚠️ 抓帧时间点可能全都 ≥ 片长（例如测试片不足 1.5s）⇒ 以前 frames 会是空列表。
    #    而 burn-full 的护栏要求 `edit/tmpimg/frame_*.png` 存在 —— 帧为空会让
    #    **烧全片被永久拦住**（护栏没有出口）。所以这里保证至少抓 1 帧：
    #    取片长中点（片长极短时取 0 点）。
    points = [t for t in TEST_FRAMES_AT if t < span] or [min(max(span / 2, 0.0), span)]
    frames: list[str] = []
    frame_failures: list[str] = []
    for t in points:
        out_png = project.tmpimg_dir / f"frame_{t}.png"
        try:
            run([FFMPEG, "-y", "-ss", str(t), "-i", str(project.subtitle_test_video),
                 "-vframes", "1", "-q:v", "2", str(out_png)])
        except Exception as e:  # noqa: BLE001
            frame_failures.append(f"{t}s: {e}")
            continue
        if out_png.is_file():
            frames.append(str(out_png))
        else:
            frame_failures.append(f"{t}s: ffmpeg 未产出 {out_png.name}")

    emit({
        "ok": bool(frames),
        "stage": "burn-test",
        # 记录本次实际用的样式覆盖值（None = 用 skill 默认），便于回退追溯
        "margin_v_override": margin_v,
        "test_video": str(project.subtitle_test_video),
        "frames": frames,
        "frame_failures": frame_failures,
        "span_seconds": span,
        "spec": probe(project.subtitle_test_video),
        "checklist": [
            "字幕是否真的烧进画面（不是黑边/空条）",
            "底部白字黑边是否遮挡人物面部或与字幕区重叠",
            "字幕时间线是否与配音对齐（尤其剪辑拼接点附近）",
            "中文是否乱码",
        ],
        "next": "视觉子代理判断通过后 burn-full",
    }, exit_code=0 if frames else 2)


def cmd_full(project: Project, *, margin_v: float | None = None) -> None:
    src, srt = project.edited_video, project.final_srt
    for p, cause in ((src, "missing_input"), (srt, "missing_srt")):
        if not p.is_file():
            emit({"ok": False, "cause": cause, "error": f"缺少 {p}"}, exit_code=2)

    dst = project.renders_dir / "subtitled_full.mp4"
    _burn(src, dst, _subtitles_filter(srt, margin_v), audio_copy=True, duration=None)

    spec_src, spec_dst = probe(src), probe(dst)
    duration_delta = round(spec_dst["duration"] - spec_src["duration"], 3)
    checks = {
        "has_video": spec_dst["has_video"],
        "has_audio": spec_dst["has_audio"],
        # 烧录不改变时长；偏差 >0.5s 视为异常
        "duration_consistent": abs(duration_delta) <= 0.5,
    }
    cause = None
    if not (checks["has_video"] and checks["has_audio"]):
        cause = "missing_stream"
    elif not checks["duration_consistent"]:
        cause = "burn_duration_mismatch"
    report = {
        # ⚠️ `ok` 必须与退出码**一致**：以前这里硬写 `True`，而 checks 不通过时下面
        #    会 `exit_code=2` ⇒ **报告说成功、进程说失败**。调用方只读 JSON 报告，
        #    于是"烧全片后时长不一致"这件事被静默放过（正是 D-1 防错网要拦的情形）。
        "ok": all(checks.values()),
        "stage": "burn-full",
        "cause": cause,
        "margin_v_override": margin_v,
        "output": str(dst),
        "source_duration": spec_src["duration"],
        "output_duration": spec_dst["duration"],
        "duration_delta": duration_delta,
        "spec": spec_dst,
        "checks": checks,
        "next": "qc",
    }
    emit(report, exit_code=0 if report["ok"] else 2)


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段4 字幕烧录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("burn-test", "burn-full"):
        p = sub.add_parser(c)
        p.add_argument("--project", required=True)
        # 字幕挡脸（subtitle_occlusion）回退的唯一可调项：离底边距离，**越大越往上**。
        # 不传就用 skill 的默认样式（MarginV=26）——其余样式参数一律不许自调。
        p.add_argument(
            "--margin-v", type=float, default=None,
            help="覆盖字幕样式的 MarginV（离底边距离，越大越往上）；不传用默认 26",
        )
    args = ap.parse_args()
    project = Project.resolve(args.project)
    project.ensure_dirs()
    fn = cmd_test if args.cmd == "burn-test" else cmd_full
    fn(project, margin_v=args.margin_v)


if __name__ == "__main__":
    guarded(main)
