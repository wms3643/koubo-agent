"""阶段 1 — 转写（确定性工具，零 LLM 判断）

skill 红线：
    · large-v3 GPU float16，⛔ 禁止降级 medium
    · `language="zh"` / `beam_size=5` / `word_timestamps=True`
    · ⛔ `vad_filter=False`（开 VAD 会把长句切碎）
    · GPU 缺 CUDA DLL 会**直接报错**（不会静默降级 CPU）—— 见 `_inject_cuda_dlls`

产出：
    edit/transcript.json   —— 段列表（含 word 级时间戳），后续全部阶段的唯一数据源
    edit/transcript.txt    —— 可读底本
    edit/transcript.srt    —— 仅供人工审阅的原始字幕
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import (  # noqa: E402
    WHISPER_MODEL_DIR, Project, emit, guarded, read_json, srt_timestamp, write_json,
)

# ---------------------------------------------------------------------------
# ⛔ 必须保持句柄存活：os.add_dll_directory 返回的对象一旦被回收，DLL 目录即失效
# ---------------------------------------------------------------------------
_DLL_HANDLES: list = []


# CUDA 运行时目录（缺一个都会在 encode 阶段炸）
_CUDA_DLL_DIRS = (
    "nvidia/cublas/bin",        # cublas64_12.dll / cublasLt64_12.dll
    "nvidia/cuda_runtime/bin",  # cudart64_12.dll
    "ctranslate2",              # cudnn64_9.dll —— cudnn 由 ctranslate2 自带，不在 nvidia 包里
)


def _inject_cuda_dlls() -> list[str]:
    """把 CUDA 运行时目录挂进 Windows 的 DLL 搜索路径。

    ⚠️ 光调 `os.add_dll_directory()` **不够**（2026-10-05 真跑踩到
    `cublas64_12.dll is not found or cannot be loaded`，而该文件明明就在被注入的目录里）：
      · DLL 之间的**依赖解析**仍看 `PATH` ⇒ 必须同时前置到 `os.environ["PATH"]`；
      · cudnn 由 ctranslate2 自带（`ctranslate2/cudnn64_9.dll`），**不在 nvidia 包里**；
      · cudnn 要到首次 `encode()` 才被 dlopen，这里**显式预载**，免得那时再失败。
    """
    site_packages = Path(sys.executable).parent.parent / "Lib" / "site-packages"
    added: list[str] = []
    for sub in _CUDA_DLL_DIRS:
        d = site_packages / sub
        if not d.is_dir():
            continue
        added.append(str(d))
        _DLL_HANDLES.append(os.add_dll_directory(str(d)))
    if added:
        os.environ["PATH"] = os.pathsep.join([*added, os.environ.get("PATH", "")])
        _preload(site_packages / "ctranslate2" / "cudnn64_9.dll")
    return added


def _preload(dll: Path) -> bool:
    """预载一个 DLL；失败返回 False（不抛异常）。"""
    if not dll.is_file():
        return False
    try:
        import ctypes  # noqa: PLC0415

        _DLL_HANDLES.append(ctypes.WinDLL(str(dll)))
        return True
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段1 转写 (faster-whisper large-v3)")
    ap.add_argument("--project", required=True, help="项目目录")
    ap.add_argument("--source", default=None, help="源视频，默认 <project>/input/raw.mp4")
    ap.add_argument("--model", default=str(WHISPER_MODEL_DIR))
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--language", default="zh")
    ap.add_argument(
        "--force", action="store_true",
        help="已有逐字稿时仍需重转（⛔ 会**覆盖**它，下游全部要重跑，慎用）",
    )
    args = ap.parse_args()

    project = Project.resolve(args.project)
    project.ensure_dirs()

    # ⛔ 幂等护栏：转写是 GPU 十几分钟的重活，而且 **transcript.json 是所有下游阶段的唯一数据源**。
    #    没有这道闸，任何 agent 手一抖调一次 transcribe 就会**静默覆盖**已有逐字稿，
    #    连带让剪辑决策、剪后时间轴、字幕全部失效 —— 而它只会看到"转写成功"。
    #    注意：`kb_run` 是"一个工具承载多个 stage"，`toolFilter` **只能按工具名过滤**、
    #    无法按 stage 限制调用者，所以这道闸只能由内核把住。
    if project.transcript_json.is_file() and not args.force:
        existing = read_json(project.transcript_json)
        segs = existing.get("segments") if isinstance(existing, dict) else existing
        emit({
            "ok": False,
            "stage": "transcribe",
            "cause": "transcript_exists",
            "error": f"{project.transcript_json} 已存在，未覆盖",
            "existing_segments": len(segs or []),
            "hint": "已有逐字稿可直接复用（下一步 kb_plan_cut）。确实要重转请显式传 "
                    "args:[\"--force\"]，并清楚下游（剪辑决策/剪后时间轴/字幕）都要重跑。",
        }, exit_code=2)
        return

    source = Path(args.source) if args.source else project.raw_video
    if not source.is_file():
        emit({"ok": False, "error": f"源视频不存在: {source}"}, exit_code=2)

    injected = _inject_cuda_dlls() if args.device == "cuda" else []

    # 延迟 import：确保 DLL 目录已注入
    from faster_whisper import WhisperModel  # noqa: PLC0415

    compute_type = "float16" if args.device == "cuda" else "int8"
    model = WhisperModel(args.model, device=args.device, compute_type=compute_type)

    segments_iter, info = model.transcribe(
        str(source),
        language=args.language,
        beam_size=5,
        word_timestamps=True,
        vad_filter=False,  # ⛔ 红线：禁止开 VAD
    )
    segments = list(segments_iter)

    payload = []
    for s in segments:
        payload.append(
            {
                "id": s.id,
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "text": (s.text or "").strip(),
                "words": [
                    {
                        "start": round(w.start, 3),
                        "end": round(w.end, 3),
                        "word": w.word,
                        "probability": round(w.probability, 4),
                    }
                    for w in (s.words or [])
                ],
            }
        )

    write_json(project.transcript_json, payload)
    project.transcript_txt.write_text(
        "\n".join(p["text"] for p in payload) + "\n", encoding="utf-8"
    )
    # 原始 SRT 仅供人工审阅（不参与成片）
    with project.transcript_srt.open("w", encoding="utf-8", newline="\n") as f:
        for i, p in enumerate(payload, 1):
            f.write(
                f"{i}\n{srt_timestamp(p['start'])} --> {srt_timestamp(p['end'])}\n"
                f"{p['text']}\n\n"
            )

    emit(
        {
            "ok": True,
            "stage": "transcribe",
            "device": args.device,
            "compute_type": compute_type,
            "cuda_dll_dirs": injected,
            "language": info.language,
            "duration": round(info.duration, 3),
            "segments": len(payload),
            "words": sum(len(p["words"]) for p in payload),
            "transcript_json": str(project.transcript_json),
            "transcript_txt": str(project.transcript_txt),
            "transcript_srt": str(project.transcript_srt),
            "next": "cut-plan",
        }
    )


if __name__ == "__main__":
    guarded(main)
