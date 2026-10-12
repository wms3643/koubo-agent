"""阶段 6 — 质检（确定性断言 + 证据产出）

定位（关键设计）：
    本模块**只做可机械判定的断言**，并把结论固化成带 `cause` 的检查项。
    "接缝听感好不好""字幕挡不挡脸"这类**主观判断交给视觉/听觉子代理**，本模块
    只负责把它们需要的证据（帧、音量统计、黑帧区间）备好。

    ⇒ 每个失败项都带 `cause`，agent 据此**路由到不同的回退目标**，而不是笼统"重做"。

cause → 回退目标（与 docs/contracts.md 的「回退矩阵」严格一致）：
    srt_bom / srt_trailing_symbol / srt_timeline_overlap   -> stage3_srt（工具重跑）
    srt_timeline_mismatch / cut_duration_mismatch           -> 剪裁策划（判断问题）
    junction_glitch / audio_clipping / av_desync            -> 剪裁策划（调切点）
    subtitle_occlusion                                      -> 字幕制作（改 force_style 重烧）
    black_frame                                             -> 剪裁策划（重跑缝接）
    missing_overlay                                         -> 特效设计（重跑 overlay-render）
    missing_stream / spec_mismatch                          -> 硬失败，升级人工
    bgm_missing / bgm_silent / bgm_overpowers_voice /
        bgm_true_peak / bgm_abrupt_cut                      -> 剪裁策划（调混音参数重跑 mix）

关于 BGM（为什么必须是数字断言）：
    ⛔ **质检员听不到声音** —— DSH 只有 `read_image`，**没有读音频的工具**。
    所以"BGM 有没有进去""有没有盖住人声""结尾是不是骤停"这三件事**不能派给 agent 去听**，
    必须在这里用 ffmpeg `ebur128` 测成数字。做法：把混音后的响度与**混音前的纯人声**对比，
    差值就是 BGM 的贡献量（`mix.json` 同时记录了 `video`（纯人声输入）与 `output`（混音产物））。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import (  # noqa: E402
    FFMPEG, FFPROBE, Project, emit, guarded, probe, read_json, run, run_merged, write_json,
)

TRAILING_CHARS = "。！？；，、：,.!?;: "
DEFAULT_FRAME_POINTS = (1.0,)
JUNCTION_WINDOW = 0.05


def _check(cid: str, ok: bool, cause: str | None, detail: str) -> dict:
    return {"id": cid, "ok": bool(ok), "cause": None if ok else cause, "detail": detail}


# ---------------------------------------------------------------------------
#  字幕层断言
# ---------------------------------------------------------------------------
def _parse_srt(path: Path) -> list[tuple[float, float, str]]:
    def to_sec(ts: str) -> float:
        h, m, rest = ts.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    items = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        m = re.match(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", lines[1])
        if not m:
            continue
        items.append((to_sec(m.group(1)), to_sec(m.group(2)), " ".join(lines[2:])))
    return items


def _srt_checks(srt: Path, video_duration: float | None, *,
                full_length: bool) -> list[dict]:
    checks: list[dict] = []
    raw = srt.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    checks.append(_check(
        "srt_no_bom", not has_bom, "srt_bom",
        "SRT 无 BOM" if not has_bom else "SRT 带 UTF-8 BOM → libass 会报 Invalid data、全片无字幕",
    ))

    items = _parse_srt(srt)
    checks.append(_check("srt_has_entries", len(items) > 0, "srt_empty",
                         f"{len(items)} 条字幕"))

    trailing = [t for _, _, t in items if t and t[-1] in TRAILING_CHARS]
    checks.append(_check(
        "srt_no_trailing_symbol", not trailing, "srt_trailing_symbol",
        f"句末带符号 {len(trailing)} 条" + (f"，例：{trailing[0][:20]}" if trailing else ""),
    ))

    overlaps = [
        (items[i - 1][1], items[i][0])
        for i in range(1, len(items)) if items[i][0] < items[i - 1][1]
    ]
    checks.append(_check(
        "srt_monotonic", not overlaps, "srt_timeline_overlap",
        f"时间重叠 {len(overlaps)} 处" + (f"，例 {overlaps[0]}" if overlaps else ""),
    ))

    # ⚠️ 这项只能在**全片**上做。
    #    以前不看质检目标：`kb_qc(target="subtitle_test")` 也会拿整份 final.srt（全片
    #    1185.9s）去比 10s 测试片 ⇒ **恒定报 srt_timeline_mismatch**，把 CP2 这一步
    #    污染成一个假 cause（而它的回退目标是"回剪裁策划"，属最贵的一类）。
    if full_length and video_duration is not None and items:
        last_end = items[-1][1]
        drift = round(last_end - video_duration, 3)
        checks.append(_check(
            "srt_timeline_matches_video", abs(drift) <= 1.0, "srt_timeline_mismatch",
            f"末条结束 {last_end:.3f}s vs 视频时长 {video_duration:.3f}s（差 {drift:+.3f}s）",
        ))
    return checks


# ---------------------------------------------------------------------------
#  剪辑层断言
# ---------------------------------------------------------------------------
def _cut_checks(project: Project, target_video: Path, target: str) -> list[dict]:
    """`cut_duration_mismatch` 只能在**剪切产物**上判。

    ⚠️ 以前拿质检对象的时长去比"保留块计划" —— 若目标是 `final`（装配后带 xfade 转场，
    时长本来就会被转场吃掉一段），就会**误报** cut_duration_mismatch，把主管引去吃一次
    "回剪裁策划复核保留块"的贵回退。改为优先量 `renders/edited_final.mp4`（真正的剪切输出）。
    """
    checks: list[dict] = []
    if not project.cut_report.is_file() or not project.cut_blocks.is_file():
        return checks
    report = read_json(project.cut_report)
    blocks = read_json(project.cut_blocks)
    planned = round(sum(b["edit_end"] - b["edit_start"] for b in blocks), 3)

    use_edited = target != "edited" and project.edited_video.is_file()
    measured_on = project.edited_video if use_edited else target_video
    measured = probe(measured_on)["duration"]
    drift = round(measured - planned, 3)
    detail = (f"实测 {measured:.3f}s（量自 {measured_on.name}）vs 计划 {planned:.3f}s"
              f"（差 {drift:+.3f}s）；已删 {report.get('dropped_segments')} 段 / "
              f"{report.get('blocks')} 块")
    if use_edited:
        tgt = probe(target_video)["duration"]
        detail += (f"；质检目标 {target_video.name} {tgt:.3f}s"
                   f"（差 {round(tgt - measured, 3):+.3f}s，装配转场造成的差值属正常）")
    checks.append(_check(
        "edited_duration_vs_plan", abs(drift) <= 0.5, "cut_duration_mismatch", detail,
    ))
    return checks


def _stream_checks(video: Path) -> tuple[list[dict], dict]:
    out = run([
        FFPROBE, "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,duration,r_frame_rate,width,height",
        "-of", "json", str(video),
    ])
    import json as _json
    data = _json.loads(out)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    checks = [_check(
        "streams_present", bool(v) and bool(a), "missing_stream",
        f"video={'有' if v else '无'} audio={'有' if a else '无'}",
    )]
    # ⚠️ 这一项**必须恒出现**（不能因为拿不到 duration 就静默跳过）：本项目的历史教训
    #    就是"断言看起来通过、其实从未运行"。拿不到流级 duration 时要**报出来**，
    #    而不是从 checks 里消失。
    if v and a:
        vd, ad = v.get("duration"), a.get("duration")
        if vd is None or ad is None:
            checks.append(_check(
                "av_duration_equal", False, "qc_probe_failed",
                "音视频轨未提供 duration，无法判定 A/V 同步"
                "（format 级总时长不能替代轨级判定）—— 不得当作通过",
            ))
        else:
            d = round(abs(float(vd) - float(ad)), 3)
            checks.append(_check(
                "av_duration_equal", d <= 0.3, "av_desync",
                f"视频轨 {float(vd):.3f}s vs 音频轨 {float(ad):.3f}s（差 {d:.3f}s）",
            ))
    return checks, {"video": v, "audio": a}


def _volume_checks(video: Path, *, has_audio: bool) -> list[dict]:
    """volumedetect：max_volume 接近 0dB 说明有削顶（爆音）。

    ⚠️ 用 `run_merged`：volumedetect 的统计写在 **stderr**，用 `run()` 会拿不到
    ⇒ 断言静默失效（历史上真的发生过：这一项曾完全没进质检报告）。

    ⚠️ 无音频流时必须**提前退出**：以前直接跑 volumedetect，ffmpeg 会以非 0 退出，
    `run_merged` 抛 RuntimeError，整个 qc 被顶层兜成 `kernel_exception`（退出码 1）——
    于是"缺音频流"这件事报的是"内核崩了"，而不是可路由的 `missing_stream`。
    """
    if not has_audio:
        return [_check(
            "audio_no_clipping", False, "missing_stream",
            "无音频流，无法检测削顶/爆音（见 streams_present 的 missing_stream）",
        )]
    try:
        text = run_merged([FFMPEG, "-hide_banner", "-nostats", "-i", str(video),
                           "-af", "volumedetect", "-f", "null", "-"])
    except RuntimeError as e:
        return [_check("audio_no_clipping", False, "qc_probe_failed",
                       f"volumedetect 执行失败：{str(e)[-200:]}")]
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", text)
    if not m:
        return [_check("audio_no_clipping", False, "qc_probe_failed",
                       "volumedetect 未返回 max_volume —— 质检探针失效，不得当作通过")]
    max_db = float(m.group(1))
    return [_check(
        "audio_no_clipping", max_db <= -0.1, "audio_clipping",
        f"max_volume = {max_db} dB" + ("（接近 0dB，疑削顶/爆音）" if max_db > -0.1 else ""),
    )]


BGM_DELTA_MIN_LU = 0.3      # BGM 至少要给人声轨加这么多响度，否则等于没进去
BGM_DELTA_MAX_LU = 4.0      # 超过就说明 BGM 抢戏、人声不清晰
TRUE_PEAK_LIMIT_DBFS = -1.0  # 真峰值上限（超过易削顶）


def _loudness(path: Path) -> dict | None:
    """用 ffmpeg ebur128 测整体响度（LUFS）与真峰值（dBFS）。

    ⚠️ 同样必须用 `run_merged` —— ebur128 的 Summary 写在 stderr。
    """
    text = run_merged([
        FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
        "-filter_complex", "ebur128=peak=true", "-f", "null", "-",
    ])
    # ⚠️ 必须锚定 Summary 段：ebur128 在开头几百毫秒会先输出几帧 `I: -70.0 LUFS`
    # 的**启动占位值**，直接取第一个匹配会永远读到 -70.0（曾实际踩到）。
    mi = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+)\s*LUFS", text)
    if mi:
        lufs = float(mi.group(1))
    else:
        tail_i = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", text)
        if not tail_i:
            return None
        lufs = float(tail_i[-1])
    mp = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+)\s*dBFS", text)
    return {
        "lufs": round(lufs, 2),
        "peak": round(float(mp.group(1)), 2) if mp else None,
    }


def _bgm_checks(project: Project) -> list[dict]:
    """只在「跑过混音」时生效：把混音产物与混音前的纯人声对比。

    `mix.json` 里 `video` = 混音前的纯人声输入，`output` = 铺完 BGM 的产物。
    两者时间轴完全一致 ⇒ 响度差就是 BGM 的贡献量。
    """
    spec_path = project.edit_dir / "mix.json"
    if not spec_path.is_file():
        return []
    spec = read_json(spec_path)
    voice = project.root / spec.get("video", "")
    mixed = project.root / spec.get("output", "renders/final.mp4")
    bgm = project.root / spec.get("bgm", "")

    # 素材缺失 ⇒ 回剪裁策划：优先自己调 kb_bgm 生成（约 0.49 元/次），
    # 用户明确指定曲目时才取回用户的文件。
    # ⚠️ 这段文案以前写的是"需要用户提供，禁止自行生成" —— 那是被修正前的旧结论，
    #    与 docs/contracts.md「BGM」一节相反（绕过了 suno-v4.5 的存在），已改回。
    if not bgm.is_file():
        return [_check("bgm_source_exists", False, "bgm_missing",
                       f"BGM 素材不存在：{bgm} —— 回剪裁策划：调 kb_bgm 生成"
                       "（suno-v4.5，约 0.49 元/次）；用户已指定曲目则取回文件")]
    if not voice.is_file() or not mixed.is_file():
        return [_check("bgm_mix_inputs", False, "bgm_missing",
                       f"混音输入缺失：voice={voice.is_file()} mixed={mixed.is_file()}")]

    lv, lm = _loudness(voice), _loudness(mixed)
    if not lv or not lm:
        return [_check("bgm_loudness_probe", False, "qc_probe_failed",
                       f"响度探针失效（voice={lv} mixed={lm}）—— 不得当作通过")]

    delta = round(lm["lufs"] - lv["lufs"], 2)
    checks = [
        _check("bgm_present", delta >= BGM_DELTA_MIN_LU, "bgm_silent",
               f"混音后 {lm['lufs']} LUFS vs 纯人声 {lv['lufs']} LUFS"
               f"（Δ{delta:+.2f} LU）"
               + ("——BGM 几乎没进去" if delta < BGM_DELTA_MIN_LU else "")),
        _check("bgm_not_overpowering", delta <= BGM_DELTA_MAX_LU, "bgm_overpowers_voice",
               f"Δ{delta:+.2f} LU（上限 {BGM_DELTA_MAX_LU}）"
               + ("——BGM 抢戏、人声不清晰，下调 section 音量" if delta > BGM_DELTA_MAX_LU else "")),
    ]
    if lm["peak"] is not None:
        checks.append(_check(
            "bgm_true_peak_ok", lm["peak"] <= TRUE_PEAK_LIMIT_DBFS, "bgm_true_peak",
            f"真峰值 {lm['peak']} dBFS（上限 {TRUE_PEAK_LIMIT_DBFS}）"
            + ("——易削顶，整体降音量" if lm["peak"] > TRUE_PEAK_LIMIT_DBFS else ""),
        ))

    # 结尾骤停：不去猜混音后的听感（那需要"听"，我们听不到），而是**直接查素材与参数** ——
    #   a) BGM 时长不够铺满（扣掉淡出）⇒ 音乐中途断了，后面只剩人声
    #   b) 没设淡出 ⇒ 音乐在片尾被硬切（听感上像咔一声）
    # ⭐ 但**开了 `bgm_loop` 就不算问题** —— Suno 单曲最长 4 分钟，口播片常 20 分钟，
    #    循环是正常做法（`mix` 用 `-stream_loop -1` 补齐）。只有显式关掉循环才检查时长。
    fade_out = float(spec.get("bgm_fade_out", 1.5))
    loop = bool(spec.get("bgm_loop", True))
    bgm_spec = probe(bgm)
    total = probe(mixed)["duration"] or 0.0
    if total and bgm_spec.get("duration"):
        have = round(bgm_spec["duration"], 3)
        if loop:
            checks.append(_check(
                "bgm_covers_duration", True, "bgm_abrupt_cut",
                f"BGM {have}s / 片长 {round(total, 3)}s —— 已开 bgm_loop，循环补齐（正常）",
            ))
        else:
            need = round(total - max(0.0, fade_out), 3)
            checks.append(_check(
                "bgm_covers_duration", have >= need - 0.2, "bgm_abrupt_cut",
                f"BGM {have}s vs 需要 {need}s（片长 {round(total, 3)}s − 淡出 {fade_out}s）"
                + ("——未开循环且音乐铺不满，中途就断了" if have < need - 0.2 else ""),
            ))
    checks.append(_check(
        "bgm_has_fade_out", fade_out > 0, "bgm_abrupt_cut",
        f"bgm_fade_out = {fade_out}s" + ("——没设淡出，片尾音乐会被硬切" if fade_out <= 0 else ""),
    ))

    # 若音乐根本没铺满，`bgm_present` 的偏低只是连带症状 —— 在 detail 里说清，
    # 免得主管把根因（音乐太短）误判成"没混进去"而白跑一轮。
    covers = next((c for c in checks if c["id"] == "bgm_covers_duration"), None)
    if covers is not None and not covers["ok"]:
        for c in checks:
            if c["id"] == "bgm_present":
                c["detail"] += "（注意：音乐本身没铺满，这是连带症状，根因看 bgm_covers_duration）"
    return checks


def _black_frame_checks(video: Path, duration: float) -> list[dict]:
    """head/tail 2s 内黑帧检测。

    ⚠️ blackdetect 的命中提示写在 **stderr** ⇒ 必须用 `run_merged`。
       历史教训：本项曾用 `run()`，于是**永远报"未检测到黑帧"** —— 看似通过、实则从未运行。
       为防再次静默失效，这里额外要求输出里出现 ffmpeg 的输出流信息（证明滤波器真跑了）。
    """
    checks = []
    for tag, ss, t in (("head", 0.0, min(2.0, duration)), ("tail", max(0.0, duration - 2.0), 2.0)):
        try:
            text = run_merged([FFMPEG, "-hide_banner", "-i", str(video),
                               "-ss", str(ss), "-t", str(t),
                               "-vf", "blackdetect=d=0.3:pix_th=0.10",
                               "-f", "null", "-"])
        except RuntimeError as e:
            text = str(e)
        ran = "Output #0" in text or "blackdetect" in text
        if not ran:
            checks.append(_check(
                f"no_black_frame_{tag}", False, "qc_probe_failed",
                f"{tag} 黑帧探针未返回可解析输出 —— 不得当作通过",
            ))
            continue
        found = "black_start" in text
        checks.append(_check(
            f"no_black_frame_{tag}", not found, "black_frame",
            f"{tag} {t}s 内{'检测到' if found else '未检测到'}黑帧",
        ))
    return checks


def _grab_frames(video: Path, points: list[float], out_dir: Path,
                 prefix: str) -> tuple[list[str], list[str]]:
    """抓帧。返回 `(成功路径, 失败说明)`。

    ⚠️ 以前失败被 `except RuntimeError: pass` **静默吞掉**，但报告里仍然列了那个路径 ——
    审片子代理按路径 `read_image` 会拿到"文件不存在"，而报告说"已抓帧"，两边对不上。
    现在失败的进 `frame_failures`，模型一眼能看到证据缺了哪张。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[str] = []
    failures: list[str] = []
    for i, t in enumerate(points):
        p = out_dir / f"{prefix}_{i:02d}_{t:.2f}s.png"
        try:
            run([FFMPEG, "-y", "-ss", str(max(0.0, t)), "-i", str(video),
                 "-vframes", "1", "-q:v", "2", str(p)])
        except RuntimeError as e:
            failures.append(f"{p.name}: {str(e)[-160:]}")
            continue
        if p.is_file():
            frames.append(str(p))
        else:
            failures.append(f"{p.name}: ffmpeg 退出 0 但未产出文件")
    return frames, failures


def cmd_qc(project: Project, target: str, frame_points: list[float]) -> None:
    video = {
        "subtitle_test": project.subtitle_test_video,
        "edited": project.edited_video,
        "final": project.final_video,
        "subtitled": project.renders_dir / "subtitled_full.mp4",
    }.get(target)
    if video is None or not video.is_file():
        emit({"ok": False, "cause": "missing_input", "error": f"质检目标不存在: {target}"},
             exit_code=2)

    spec = probe(video)
    stream_checks, streams = _stream_checks(video)
    has_audio = bool(streams.get("audio"))
    # ⚠️ 只有"全片"才谈得上"字幕时间轴与视频时长是否吻合"：
    #    subtitle_test 是 10s 测试片，拿全片 SRT 去比会**恒定误报** srt_timeline_mismatch。
    full_length = target in ("edited", "subtitled", "final")

    checks: list[dict] = []
    checks += stream_checks
    checks += _volume_checks(video, has_audio=has_audio)
    checks += _black_frame_checks(video, spec["duration"])

    if project.final_srt.is_file():
        checks += _srt_checks(project.final_srt, spec["duration"], full_length=full_length)
    if full_length:
        checks += _cut_checks(project, video, target)
    if target == "final":
        # BGM 只在混音后存在；质检员听不到声音 ⇒ 由内核测数字
        checks += _bgm_checks(project)

    # 抓帧：默认在给定时间点；有保留块时补切点前后帧（接缝证据）
    junction_points: list[float] = []
    if project.cut_blocks.is_file():
        blocks = read_json(project.cut_blocks)
        for b in blocks[1:]:
            junction_points += [
                round(b["edit_start"] - JUNCTION_WINDOW, 3),
                round(b["edit_start"] + JUNCTION_WINDOW, 3),
            ]
    frames, frame_failures = _grab_frames(video, frame_points, project.tmpimg_dir, "qc")
    junctions, junction_failures = _grab_frames(
        video, junction_points, project.tmpimg_dir, "junction")
    if frame_failures or junction_failures:
        # 取证失败也要变成**可路由的检查项**，而不是只在 frames 里少一项
        checks.append(_check(
            "frames_grabbed", False, "qc_probe_failed",
            f"抓帧失败 {len(frame_failures)} / 切点帧失败 {len(junction_failures)}"
            + (f"；例：{(frame_failures + junction_failures)[0]}" if (frame_failures + junction_failures) else ""),
        ))

    failed = [c for c in checks if not c["ok"]]
    report = {
        "ok": not failed,
        "stage": "qc",
        "target": target,
        "video": str(video),
        "spec": spec,
        "checks": checks,
        "failed_checks": [c for c in failed],
        "causes": sorted({c["cause"] for c in failed if c["cause"]}),
        "frames": frames,
        "junction_frames": junctions,
        "frame_failures": frame_failures + junction_failures,
        "vision_review_required": [
            "字幕是否遮挡人物面部/字幕区是否与动效层重叠",
            "剪辑拼接点前后口型/画面是否跳帧、呼吸是否被截断",
            "首尾是否有黑帧或静止",
        ],
        "route_hint": (
            "causes 为空即通过；否则按 docs/contracts.md「回退矩阵」把每个 cause 映射到"
            "唯一回退目标（工具重跑优先于 LLM 重做）。"
        ),
    }
    write_json(project.qc_report, report)
    emit(report, exit_code=0 if report["ok"] else 2)


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段6 质检")
    ap.add_argument("--project", required=True)
    ap.add_argument("--target", default="final",
                    choices=["subtitle_test", "edited", "subtitled", "final"])
    ap.add_argument("--frames-at", default="", help="逗号分隔的秒数，默认 1.0")
    args = ap.parse_args()
    project = Project.resolve(args.project)
    points = (
        [float(x) for x in args.frames_at.split(",") if x.strip()]
        if args.frames_at else list(DEFAULT_FRAME_POINTS)
    )
    cmd_qc(project, args.target, points)


if __name__ == "__main__":
    guarded(main)
