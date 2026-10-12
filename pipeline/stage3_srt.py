"""阶段 3 — 字幕（确定性工具，零 LLM 判断）

skill 红线（逐条对应到代码）：
    · 字幕行 = Whisper 保留段（已合并幻觉链）本身；⛔ 不做碎段合并、⛔ 不做 50% 逗号折行
    · 时间重映射容差 0.6，⛔ 绝不 clamp 到块边界（否则边界段字幕挤堆、与发声脱节）
    · 累积幻觉链合并：`gap <= 0.45` 且后段是前段前缀/相等 → 合并，留最长文本
    · 句末禁一切符号：`rstrip('。！？；，、：,.!?;: ')`；句中符号保留
    · 单调化：**只推后后一条，绝不砍前一条**
    · SRT 必须 UTF-8 **无 BOM**（带 BOM → libass 报 Invalid data → 全片无字幕）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import (  # noqa: E402
    HALLUCINATION_GAP, MAX_SUBTITLE_CHARS_WARN, MIN_SUBTITLE_SECONDS,
    SUBTITLE_FORCE_STYLE, KernelError, Project, emit, guarded,
    load_segments, probe, read_json, remap_time, srt_timestamp, write_json,
)

TRAILING_CHARS = "。！？；，、：,.!?;: "


def _norm(s: str) -> str:
    return s.replace(" ", "").replace("\u3000", "")


def build_srt(project: Project) -> dict:
    segs = load_segments(project.transcript_json)
    blocks = read_json(project.cut_blocks)
    # 文本修正来自两个子代理，各自拥有一个文件（见 Project.subtitle_fixes 的注释）。
    # 逐字稿编辑写 edit_decisions.json，字幕制作写 subtitle_fixes.json；后者优先级更高。
    text_fixes: dict[int, str] = {}
    for src in (project.edit_decisions, project.subtitle_fixes):
        if src.is_file():
            raw = read_json(src).get("text_fixes") or {}
            for k, v in raw.items():
                try:
                    text_fixes[int(k)] = str(v)
                except (TypeError, ValueError) as e:
                    # 键不是数字 ⇒ 是"文件写错"，不是内核崩了，给可路由 cause（退出码 2）
                    raise KernelError(
                        "bad_spec",
                        f"{src.name} 的 text_fixes 键必须是 0-based 段序号，实际 {k!r}（{e}）",
                    ) from None

    # 保留段索引（块内 first_seg..last_seg 为 1-based）
    keep_idx: list[int] = []
    for b in blocks:
        keep_idx.extend(range(b["first_seg"] - 1, b["last_seg"]))
    total_segments = len(segs)

    # 原始秒 -> 剪辑后秒（容差 0.6，绝不 clamp）
    # ⭐ 复用 common.remap_time —— 唯一的重映射实现。以前这里另写了一份同样的循环，
    #    与 build_timeline 里的那份会各自漂移（容差/是否 clamp 一改就对不上）。
    def remap(t: float) -> float | None:
        return remap_time(blocks, t)

    # ── 工序1 收集保留段的 word 级时间（同时保留 words，供时间轴索引复用）──
    units = []
    for i in keep_idx:
        s = segs[i]
        txt = text_fixes.get(i, (s.get("text") or "")).strip()
        if not txt:
            continue
        words = [w for w in (s.get("words") or []) if (w.get("word") or "").strip()]
        start = words[0]["start"] if words else s["start"]
        end = words[-1]["end"] if words else s["end"]
        units.append({
            "seg_idx": i,
            "start": start,
            "end": end,
            "text": txt,
            "words": [
                {"w": (w.get("word") or "").strip(), "s": w.get("start"), "e": w.get("end")}
                for w in words
            ],
        })

    # ── 工序2 累积幻觉链合并（时间轴不动，只在字幕层收敛）──
    chain: list[dict] = []
    for u in units:
        if chain:
            p = chain[-1]
            gap = u["start"] - p["end"]
            a, b = _norm(p["text"]), _norm(u["text"])
            if gap <= HALLUCINATION_GAP and (b == a or b.startswith(a)):
                if len(b) >= len(a):
                    p["text"] = u["text"]
                    # ⚠️ 文本变长，**词级时间必须跟着延长**：否则合并后的字幕文本
                    #    比 words 覆盖的时间长，动效按词对齐会漏掉后半句（静默错位）。
                    p["words"] = (p.get("words") or []) + (u.get("words") or [])
                p["end"] = u["end"]
                continue
        chain.append(dict(u))
    merged_hallucinations = len(units) - len(chain)
    units = chain

    # ── 工序3/4 已废弃：不做碎段合并、不做长句折行 ──
    # 带上 words：时间轴索引要与字幕**逐条 1:1**（动效按字幕行设计最自然）
    sentences = [(u["start"], u["end"], u["text"], u.get("words") or []) for u in units]

    # ── 工序5 重映射 + 句末去符号 + 过滤单字 ──
    out: list[list] = []
    dropped_offblock: list[dict] = []
    for st, en, t, ws in sentences:
        t = t.strip().rstrip(TRAILING_CHARS)
        if len(t) <= 1:
            continue
        ns, ne = remap(st), remap(en)
        if ns is None or ne is None:
            dropped_offblock.append({
                "text": t,
                "orig_start": round(st, 3),
                "orig_end": round(en, 3),
                "duration": round(en - st, 3),
            })
            continue
        if ne <= ns:
            ne = ns + 0.3
        # 词级时间同步重映射（供时间轴索引；动效层用它做精确到词的动画）
        tl_words = []
        for w in ws:
            a, b = remap(w["s"]), remap(w["e"])
            if a is None or b is None:
                continue
            tl_words.append({"w": w["w"], "s": round(a, 3), "e": round(b, 3)})
        # ⭐ 一条记录里同时给出**两套时间轴**：
        #    source_* = 剪辑前（原片）坐标 —— 回答"这句原来在哪"，用于追溯与回退
        #    edit_*   = 剪辑后（成片）坐标 —— 回答"动效该在第几秒"，用于音画同步
        out.append({
            "source_start": round(st, 3),
            "source_end": round(en, 3),
            "edit_start": ns,
            "edit_end": ne,
            "text": t,
            "words": tl_words,
        })

    if not out:
        # ⚠️ 失败也要**落盘报告**：契约要求 agent 以写盘的报告为准（终端中文可能不显示），
        #    之前这条失败路径只回 stdout，agent 去看 srt_report.json 会读到上一轮的旧内容。
        fail = {
            "ok": False, "stage": "srt", "cause": "empty_srt",
            "error": "重映射后无字幕条目",
            "kept_segments": len(keep_idx),
            "total_segments": total_segments,
            "dropped_off_block": len(dropped_offblock),
            "checks": {"no_bom": None, "no_trailing_symbol": None, "ends_within_video": None},
        }
        write_json(project.srt_report, fail)
        return fail

    # ── 工序6 单调化：只推后后一条，绝不砍前一条 ──
    out.sort(key=lambda x: x["edit_start"])
    for k in range(1, len(out)):
        if out[k]["edit_start"] < out[k - 1]["edit_end"]:
            out[k]["edit_start"] = out[k - 1]["edit_end"] + 0.01
        if out[k]["edit_end"] < out[k]["edit_start"] + MIN_SUBTITLE_SECONDS:
            out[k]["edit_end"] = out[k]["edit_start"] + MIN_SUBTITLE_SECONDS

    # 注：「剪后时间轴」**不由本阶段产出** —— 它是**剪辑的产物**，由剪裁策划在 cut-apply
    # 完成时建立（见 stage2_cut.py 与 edit/timeline_map.json）。本阶段只消费 cut_blocks。

    # ── 写 SRT：UTF-8 无 BOM，LF 换行 ──
    project.final_srt.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for n, o in enumerate(out, 1):
        lines += [
            str(n),
            f"{srt_timestamp(o['edit_start'])} --> {srt_timestamp(o['edit_end'])}",
            o["text"],
            "",
        ]
    project.final_srt.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    # ── 自检（供 qc / 人工交叉验证）──
    raw_bytes = project.final_srt.read_bytes()
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    trailing_bad = [o["text"] for o in out if o["text"] and o["text"][-1] in TRAILING_CHARS]
    long_lines = [o["text"] for o in out if len(o["text"]) > MAX_SUBTITLE_CHARS_WARN]
    video_duration = None
    if project.edited_video.is_file():
        video_duration = probe(project.edited_video)["duration"]

    report = {
        "ok": True,
        "stage": "srt",
        "kept_segments": len(keep_idx),
        "total_segments": total_segments,
        "merged_hallucinations": merged_hallucinations,
        "dropped_off_block": len(dropped_offblock),
        "off_block_segments": dropped_offblock[:80],
        "off_block_truncated": len(dropped_offblock) > 80,
        "text_fixes_applied": len(text_fixes),
        "timeline_map": str(project.timeline_map),
        "timeline_map_present": project.timeline_map.is_file(),
        "subtitle_count": len(out),
        "srt_path": str(project.final_srt),
        "srt_encoding": "utf-8",
        "has_bom": has_bom,
        "trailing_symbol_count": len(trailing_bad),
        "over_20_chars_count": len(long_lines),
        "timeline_start": srt_timestamp(out[0]["edit_start"]),
        "timeline_end": srt_timestamp(out[-1]["edit_end"]),
        "video_duration": video_duration,
        "force_style": SUBTITLE_FORCE_STYLE,
        "checks": {
            "no_bom": not has_bom,
            "no_trailing_symbol": not trailing_bad,
            "ends_within_video": (
                video_duration is None or out[-1]["edit_end"] <= video_duration + 1.0
            ),
        },
    }
    write_json(project.srt_report, report)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段3 生成成片字幕 (final.srt)")
    ap.add_argument("--project", required=True)
    args = ap.parse_args()
    project = Project.resolve(args.project)
    report = guarded(build_srt, project)
    if not report:
        return
    ok = report.get("ok") and all(report.get("checks", {}).values())
    emit({**report, "next": "burn"}, exit_code=0 if ok else 2)


if __name__ == "__main__":
    guarded(main)
