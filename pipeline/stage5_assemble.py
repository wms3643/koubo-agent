"""阶段 5 — 装配（开场/章节页 xfade 缝接 + BGM + 成片）

skill 红线：
    · FFmpeg 8.x `acrossfade` 是 overlap 语义，⛔ 不写 `offset=`（旧选项已移除）
    · BGM 直接 `atrim`，⛔ 不加 `apad`（会补无限长静音 → amix 误判长度 → 跑到 48 分钟才失败）
    · 多源统一 `fps=30,settb=1/15360` 防时基 mismatch
    · 巨量分段必须用 `-filter_complex_script 文件`，⛔ 不写 `-filter_complex "$(cat fg.txt)"`（超 ARG_MAX）
    · 线性 xfade 链是 O(n²) ⇒ 段数多时**必须分块**（本实现按二分递归分块）
    · 长渲染放后台任务（由 DSH 侧 `run_in_background` 承担）

⚠️ 与本 skill 的已知偏离（需用户拍板，见 docs/contracts.md「偏离登记」）：
    skill 正文的递推式 `acc = acc + 前段时长; off = acc - 0.5` 在 **2 段场景**正确；
    3 段及以上我改用官方 xfade 语义 `off_k = 已累积输出时长 - T`，因为 xfade 的 offset 是
    相对第一路（累积输出）时间轴的，用前者会让第 3 段起的转场落在累积输出的末尾之外。
    为免静默出错，生成后会**预测总时长并与实测比对**，偏差 > 0.5s 即报错。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import (  # noqa: E402
    FFMPEG, KernelError, Project, emit, guarded, probe, read_json, run, write_json,
)

TARGET_FPS = 30
SETTB = "1/15360"
XFADE_ONEPASS_MAX = 12   # 单次 filter 图内最多串联的段数（超过则分块）
DEFAULT_TRANSITION = 0.5
DEFAULT_BGM_VOLUME = 0.12


def _norm_vf(idx: int, w: int, h: int) -> str:
    return (
        f"[{idx}:v]scale={w}:{h}:flags=lanczos,setsar=1,"
        f"fps={TARGET_FPS},settb={SETTB}[v{idx}]"
    )


def _render_chain(
    inputs: list[Path],
    transition: float,
    out: Path,
    *,
    width: int,
    height: int,
    workdir: Path,
) -> float:
    """把 N 段视频用 xfade 缝成一段；N 过大时二分递归分块，避免 O(n²) 与超长命令行。

    返回预测输出时长。
    """
    durations = [probe(p)["duration"] for p in inputs]

    if len(inputs) == 1:
        run([FFMPEG, "-y", "-i", str(inputs[0]), "-c", "copy", str(out)])
        return durations[0]

    if len(inputs) > XFADE_ONEPASS_MAX:
        mid = len(inputs) // 2
        left = workdir / f"_chunk_{out.stem}_L.mp4"
        right = workdir / f"_chunk_{out.stem}_R.mp4"
        _render_chain(inputs[:mid], transition, left,
                      width=width, height=height, workdir=workdir)
        _render_chain(inputs[mid:], transition, right,
                      width=width, height=height, workdir=workdir)
        return _render_chain([left, right], transition, out,
                             width=width, height=height, workdir=workdir)

    # ── 单次 xfade 链 ──
    parts = [_norm_vf(i, width, height) for i in range(len(inputs))]
    acc = durations[0]
    prev = "[v0]"
    for i in range(1, len(inputs)):
        offset = round(acc - transition, 3)     # 官方语义：转场起点 = 累积输出时长 − T
        if offset < 0:
            raise RuntimeError(
                f"转场时长 {transition}s 超过第 {i} 段前的累积时长 {acc}s，无法缝接"
            )
        label = f"[vx{i}]"
        parts.append(
            f"{prev}[v{i}]xfade=transition=fade:duration={transition}:offset={offset}{label}"
        )
        prev = label
        acc = round(offset + durations[i], 3)   # 新累积时长 = 转场起点 + 新段时长

    fc_file = workdir / f"_filter_{out.stem}.txt"
    fc_file.write_text(";".join(parts), encoding="utf-8", newline="\n")

    cmd = [FFMPEG, "-y"]
    for p in inputs:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex_script", str(fc_file),
        "-map", prev,
        "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(TARGET_FPS),
        str(out),
    ]
    run(cmd)
    return acc


def _apply_overlays(
    base: Path, overlays: list[dict], out: Path, *, fps: int
) -> Path:
    """把透明动效层（ProRes4444 带 alpha）叠加到基础成片上。

    音画同步的关键就在这里 —— 也是它**简单**的原因：
    动效层是按**剪辑后时间轴**渲染的（时间取自 `edit/timeline_map.json`），
    所以叠加时只需 `overlay=0:0` 从 0 点对齐，**不需要任何时间换算**。
    同步是"渲染时就对齐"的，不是"合成时再修"的。

    `overlays[]` 每项 `{path, start?, end?}`：`end` 缺省表示覆盖全片。
    """
    inputs: list[str] = ["-i", str(base)]
    for ov in overlays:
        inputs += ["-i", str(ov["path"])]

    parts: list[str] = []
    cur = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        label = f"[v{idx}]"
        end = ov.get("end")
        if end is None:
            filt = f"{cur}[{idx}:v]overlay=0:0:format=auto{label}"
        else:
            s = float(ov.get("start") or 0.0)
            filt = (
                f"{cur}[{idx}:v]overlay=0:0:format=auto:"
                f"enable='between(t,{s},{end})'{label}"
            )
        parts.append(filt)
        cur = label

    cmd = [
        FFMPEG, "-y", "-v", "error", *inputs,
        "-filter_complex", ";".join(parts),
        "-map", cur, "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "copy",
        str(out),
    ]
    run(cmd)
    return out


def _predict_duration(durations: list[float], transition: float) -> float:
    """按与 `_render_chain` **同一套递推**预测总时长：`acc_k = acc_{k-1} - T + d_k`。

    与 `_render_chain` 保持一致的唯一办法就是"两边都别自己算" —— 所以这里抽出来，
    干跑（`--dry-run`）与实跑用同一个公式。递推式见 docs/contracts.md 偏离登记 D-1。
    """
    acc = durations[0]
    for i, d in enumerate(durations[1:], start=1):
        if acc < transition:
            raise KernelError(
                "bad_spec",
                f"转场时长 {transition}s 超过第 {i} 段前的累积时长 {acc}s，无法缝接",
            )
        acc = round(acc - transition + d, 3)
    return acc


def cmd_assemble(project: Project, spec_path: Path, *, dry_run: bool) -> None:
    spec = read_json(spec_path)

    segments_in = spec.get("segments") or []
    if len(segments_in) < 1:
        raise KernelError("bad_spec", "assemble 规格缺少 segments")
    paths = [project.root / s["path"] for s in segments_in]
    for p in paths:
        if not p.is_file():
            raise KernelError("missing_input", f"缺少片段 {p}")

    try:
        transition = float(spec.get("transition", DEFAULT_TRANSITION))
    except (TypeError, ValueError):
        raise KernelError(
            "bad_spec", f"transition 必须是数字，实际 {spec.get('transition')!r}") from None
    if transition < 0:
        raise KernelError("bad_spec", f"transition 不能为负：{transition}")

    output = project.root / spec.get("output", "renders/final.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    workdir = project.edit_dir / "_assemble"
    workdir.mkdir(parents=True, exist_ok=True)

    first = probe(paths[0])
    width, height = first["width"] or 1920, first["height"] or 1080

    # ── 叠加层前置校验（干跑也要查 —— 否则"干跑通过、实跑才炸"）──
    overlays: list[dict] = []
    for ov in spec.get("overlays") or []:
        p = project.root / ov["path"]
        if not p.is_file():
            raise KernelError("missing_overlay", f"缺少动效层文件 {p}")
        overlays.append(
            {"path": str(p), "start": ov.get("start", 0.0), "end": ov.get("end")}
        )

    durations = [probe(p)["duration"] for p in paths]
    predicted = _predict_duration(durations, transition)

    # ── ⭐ 真干跑：只校验 + 预测，**不跑 ffmpeg、不产出文件** ──
    #    此前 `--dry-run` 只被写进报告，渲染照跑（参数名骗人）—— 已修。
    if dry_run:
        report = {
            "ok": True,
            "stage": "assemble",
            "dry_run": True,
            "would_write": str(output),
            "segments": [str(p) for p in paths],
            "overlays": [ov["path"] for ov in overlays],
            "transition": transition,
            "segment_durations": durations,
            "predicted_duration": round(predicted, 3),
            "checks": {"spec_ok": True, "duration_close": True},
            "next": "确认无误后去掉 --dry-run 真跑",
        }
        write_json(project.edit_dir / "assemble_report.json", report)
        emit(report)
        return

    try:
        rendered = _render_chain(
            paths, transition, output, width=width, height=height, workdir=workdir
        )
    except RuntimeError as e:
        # 段太短 / ffmpeg 失败 ⇒ 属于"规格或输入不对"，给可路由 cause（退出码 2），
        # 不要当成内核崩了（退出码 1）让模型去看 traceback。
        raise KernelError("bad_spec", str(e)) from None

    if overlays:
        base_tmp = output.with_name(output.stem + "_base" + output.suffix)
        if base_tmp.exists():
            base_tmp.unlink()
        output.rename(base_tmp)
        _apply_overlays(base_tmp, overlays, output, fps=TARGET_FPS)

    spec_out = probe(output)
    actual = spec_out["duration"]

    bgm_note = None
    bgm_used = False
    if spec.get("bgm"):
        bgm_note = (
            "BGM 混音需在无 BGM 主轨上二次执行；"
            "请以 bgm 参数重跑 assemble 的 mix 子命令（见 docs/contracts.md）"
        )

    checks = {
        "spec_ok": spec_out["has_video"],
        # ⚠️ 这项是 D-1（xfade 递推式偏离）的**防错网**：预测与实测偏差 > 0.5s
        #    说明递推式或切点算错了，绝不能静默出片。
        "duration_close": abs(actual - rendered) <= 0.5,
    }
    cause = None
    if not checks["spec_ok"]:
        cause = "missing_stream"
    elif not checks["duration_close"]:
        cause = "assemble_duration_mismatch"
    report = {
        # ⚠️ `ok` 必须与退出码**一致**。以前这里硬写 `True`，而下面 checks 不通过时
        #    会 `exit_code=2` ⇒ **报告说成功、进程说失败**。调用方（agent / 编排层）
        #    只读 JSON 报告 ⇒ 这就是一次静默出片，而且恰好漏掉最该拦的那次。
        "ok": all(checks.values()),
        "stage": "assemble",
        "cause": cause,
        "segments": [str(p) for p in paths],
        "overlays": [ov["path"] for ov in overlays],
        "transition": transition,
        "output": str(output),
        "predicted_duration": round(rendered, 3),
        "actual_duration": actual,
        "duration_delta": round(actual - rendered, 3),
        "bgm_used": bgm_used,
        "bgm_note": bgm_note,
        "checks": checks,
        "dry_run": False,
        "next": "qc",
    }
    write_json(project.edit_dir / "assemble_report.json", report)
    emit(report, exit_code=0 if report["ok"] else 2)


def cmd_mix(project: Project, spec_path: Path) -> None:
    """给已缝接好的视频铺 BGM：人声优先，`amix normalize=0`；BGM 用 atrim 不 apad。"""
    spec = read_json(spec_path)
    if not spec.get("video") or not spec.get("bgm"):
        raise KernelError(
            "bad_spec",
            "mix.json 必须含 video（纯人声主轨）与 bgm（素材路径），"
            f"实际 video={spec.get('video')!r} bgm={spec.get('bgm')!r}",
        )
    video = project.root / spec["video"]
    bgm = project.root / spec["bgm"]
    output = project.root / spec.get("output", "renders/final.mp4")
    # ⚠️ 输入缺失要给**可路由 cause**，而不是让 ffmpeg 抛一句 exit=1：
    #    `bgm_missing` 在回退矩阵里明确对应"回剪裁策划跑 bgm-generate / 取回用户曲目"。
    if not video.is_file():
        raise KernelError("missing_input", f"mix 的主视频不存在：{video}")
    if not bgm.is_file():
        raise KernelError(
            "bgm_missing",
            f"BGM 素材不存在：{bgm} —— 先跑 kb_bgm 生成（约 0.49 元/次），"
            "或请用户提供曲目后放进 bgm/",
        )
    sections = spec.get("bgm_volume_sections") or [
        {"start": 0.0, "end": 9999.0, "volume": spec.get("bgm_volume", DEFAULT_BGM_VOLUME)}
    ]
    total = probe(video)["duration"]

    vol_chain = "".join(
        f",volume={s['volume']}:enable='between(t,{s['start']},{s['end']})'" for s in sections
    )
    # 淡入/淡出：默认淡出 1.5s、淡入 0.5s。
    # 不淡出的话 BGM 会在片尾被"切断"（听感上像咔一声）—— 这就是 `bgm_abrupt_cut` 的修法。
    fade_in = float(spec.get("bgm_fade_in", 0.5))
    fade_out = float(spec.get("bgm_fade_out", 1.5))
    fade_chain = ""
    if fade_in > 0:
        fade_chain += f",afade=t=in:st=0:d={fade_in}"
    if fade_out > 0:
        fade_chain += f",afade=t=out:st={max(0.0, total - fade_out)}:d={fade_out}"
    bgm_chain = f"aresample=48000,aformat=sample_fmts=fltp{vol_chain}{fade_chain}"
    fc = (
        f"[1:a]{bgm_chain},atrim=0:{total}[bgm];"
        f"[0:a]aresample=48000,aformat=sample_fmts=fltp[voice];"
        f"[voice][bgm]amix=inputs=2:duration=first:normalize=0[a]"
    )
    fc_file = project.edit_dir / "_mix_filter.txt"
    fc_file.write_text(fc, encoding="utf-8", newline="\n")

    # ⭐ 循环：Suno 单曲最长 4 分钟，而口播片常 20 分钟 —— 不循环音乐会在中途断掉
    #    （`bgm_abrupt_cut`）。默认开 `-stream_loop -1`，再靠上面的 atrim 截到片长。
    loop = bool(spec.get("bgm_loop", True))
    loop_args = ["-stream_loop", "-1"] if loop else []

    run([
        FFMPEG, "-y", "-i", str(video), *loop_args, "-i", str(bgm),
        "-filter_complex_script", str(fc_file),
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-t", str(total), str(output),
    ])

    spec_out = probe(output)
    checks = {
        # 混音后必须真的有音轨，且时长没被 BGM 带跑
        "has_audio": spec_out["has_audio"],
        "duration_consistent": abs(spec_out["duration"] - total) <= 0.5,
    }
    cause = None
    if not checks["has_audio"]:
        cause = "missing_stream"
    elif not checks["duration_consistent"]:
        cause = "mix_duration_mismatch"
    report = {
        # ⚠️ 同 cmd_assemble：`ok` 必须与退出码一致（以前硬写 True ⇒ 静默通过）
        "ok": all(checks.values()),
        "stage": "mix",
        "cause": cause,
        "output": str(output),
        "duration": spec_out["duration"],
        "sections": sections,
        "fade": {"in": fade_in, "out": fade_out},
        "bgm_loop": loop,
        "checks": checks,
        "next": "qc",
    }
    write_json(project.edit_dir / "mix_report.json", report)
    emit(report, exit_code=0 if report["ok"] else 2)


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段5 装配 (xfade + BGM)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_a = sub.add_parser("assemble", help="xfade 缝接多段")
    p_a.add_argument("--project", required=True)
    p_a.add_argument("--spec", required=True)
    p_a.add_argument("--dry-run", action="store_true")
    p_m = sub.add_parser("mix", help="铺 BGM")
    p_m.add_argument("--project", required=True)
    p_m.add_argument("--spec", required=True)
    args = ap.parse_args()

    project = Project.resolve(args.project)
    project.ensure_dirs()
    spec = Path(args.spec)
    if not spec.is_absolute():
        spec = (project.root / spec).resolve()
    (cmd_assemble(project, spec, dry_run=args.dry_run) if args.cmd == "assemble"
     else cmd_mix(project, spec))


if __name__ == "__main__":
    guarded(main)
