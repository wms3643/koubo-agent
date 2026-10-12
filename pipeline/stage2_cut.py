"""阶段 2 — 剪裁（两段式：证据 → 决策 → 机械执行）

分工是本流水线的核心设计：
    `plan`  纯确定性：把转写段表 + 六类必删关键词命中 + 幻觉嫌疑 + 长空白 汇总成
            `edit/plan_context.json`，**只给证据，不给结论**。
    `apply` 纯机械：读 LLM 写出的 `edit/edit_decisions.json`，落地删除集并断言越界，
            产出逐字稿 / 保留块 / FFmpeg 裁切脚本。

skill 红线（apply 阶段强制）：
    · 删除集 **0-based**，直连 transcript.json 下标
    · 删完必须 `assert max(del) < N` 防越界
    · 保留块合并：索引连续且段间 `gap <= 5s` 才并入同块
    · 幻觉段（0.1~0.5s 长句 / 渐进累积重复）**时间轴绝不裁** —— 由阶段3 在字幕层合并
    · 编码参数照抄：libx264 / crf 18 / preset medium / aac 192k / avoid_negative_ts make_zero
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import (  # noqa: E402
    AUDIO_BITRATE, CRF_CUT, CUT_KEYWORDS, GAP_MERGE_SECONDS, PRESET_CUT,
    KernelError, Project, build_timeline, emit, guarded,
    is_suspect_hallucination, load_segments, read_json, run_bash_script,
    write_json,
)


def _parse_text_fixes(raw: object, *, where: str) -> dict[int, str]:
    """把 `text_fixes` 的键安全转成 int。

    ⚠️ 以前这里是 `{int(k): v ...}` —— 键一旦不是数字（agent 手抖写成 `"idx-3"`）
    就抛 ValueError，被顶层兜成 `kernel_exception`（退出码 1，"需人工看 traceback"）。
    可这明明是**决策文件写错了**，属于可路由的业务失败：给 `bad_spec` + 指出坏键，
    主管改文件重跑即可，不该升级成人看栈。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise KernelError(
            "bad_spec", f"{where} 的 text_fixes 必须是对象，实际 {type(raw).__name__}")
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            raise KernelError(
                "bad_spec",
                f"{where} 的 text_fixes 键必须是 0-based 段序号，实际 {k!r}",
            ) from None
    return out


# ===========================================================================
#  plan —— 产出判断依据（不做任何删除决定）
# ===========================================================================
def _keyword_hits(text: str) -> list[str]:
    return [k for k in CUT_KEYWORDS if k in text]


def cmd_plan(project: Project) -> None:
    segs = load_segments(project.transcript_json)
    n = len(segs)

    rows = []
    for i, s in enumerate(segs):
        text = (s.get("text") or "").strip()
        dur = round(s["end"] - s["start"], 3)
        # 幻觉嫌疑：判定**唯一实现**在 common（plan / apply 共用，防规则漂移）
        chars = len(text.replace(" ", ""))
        suspect_halluc = is_suspect_hallucination(s)
        rows.append(
            {
                "idx": i,  # ⚠️ 0-based，直接就是删除集里要写的数字
                "start": round(s["start"], 3),
                "end": round(s["end"], 3),
                "dur": dur,
                "chars": chars,
                "cps": round(chars / dur, 2) if dur > 0 else None,
                "text": text,
                "keywords": _keyword_hits(text),
                "suspect_hallucination": suspect_halluc,
            }
        )

    # 相邻段长空白（skill 六类必删之六：相邻保留段 >5s 静默）
    gaps = []
    for i in range(1, n):
        gap = round(segs[i]["start"] - segs[i - 1]["end"], 3)
        if gap > 1.0:
            gaps.append(
                {"after_idx": i - 1, "before_idx": i, "gap": gap, "over_5s": gap > 5.0}
            )

    # 重复文本簇（试讲重复块的线索：同概念多次出现）
    buckets: dict[str, list[int]] = {}
    for r in rows:
        key = r["text"].replace(" ", "")[:12]
        if len(key) >= 6:
            buckets.setdefault(key, []).append(r["idx"])
    repeats = [
        {"prefix": k, "idx": v, "count": len(v)} for k, v in buckets.items() if len(v) >= 2
    ]

    payload = {
        "stage": "cut-plan",
        "segment_count": n,
        "source_duration": round(segs[-1]["end"], 3) if n else 0.0,
        "instructions": (
            "通读 segments 后写出 edit_decisions.json："
            "{\"source\":\"input/raw.mp4\",\"drop_segment_ids\":[...0-based...],"
            "\"text_fixes\":{\"<idx>\":\"<改后文本>\"},\"notes\":\"...\"}。"
            "判断标准只看『对观众有无价值』；试讲重复块整段删并保留后面正式版；"
            "卡壳口误从时间轴删；suspect_hallucination=true 的段绝不删（字幕层处理）——"
            "这一条**由内核强制**：cut-apply 见到删除集里含幻觉嫌疑段会直接以 "
            "cause=hallucination_cut_forbidden 拒绝（例外须显式 --allow-hallucination-cut）。"
        ),
        "delete_keywords": CUT_KEYWORDS,
        "segments": rows,
        "long_gaps": gaps,
        "repeat_clusters": repeats,
        "stats": {
            "keyword_hit_segments": sum(1 for r in rows if r["keywords"]),
            "suspect_hallucination_segments": sum(
                1 for r in rows if r["suspect_hallucination"]
            ),
            "silence_over_5s": sum(1 for g in gaps if g["over_5s"]),
        },
    }
    write_json(project.plan_context, payload)
    emit(
        {
            "ok": True,
            "stage": "cut-plan",
            "plan_context": str(project.plan_context),
            "segment_count": n,
            "source_duration": payload["source_duration"],
            "stats": payload["stats"],
            "next": "cut-apply（需先写出 edit_decisions.json）",
        }
    )


# ===========================================================================
#  apply —— 机械落地删除集 + 断言 + 生成裁切命令
# ===========================================================================
def cmd_apply(project: Project, *, do_run: bool, allow_hallucination_cut: bool) -> None:
    segs = load_segments(project.transcript_json)
    n = len(segs)
    decisions = read_json(project.edit_decisions)

    try:
        drop = {int(x) for x in decisions.get("drop_segment_ids", [])}
    except (TypeError, ValueError) as e:
        raise KernelError(
            "bad_spec", f"drop_segment_ids 必须是 0-based 整数数组（{e}）") from None
    text_fixes = _parse_text_fixes(decisions.get("text_fixes"), where="edit_decisions.json")

    # ⛔ 红线：越界检查必须在任何写入之前
    if drop:
        bad = [d for d in drop if d < 0 or d >= n]
        if bad:
            emit(
                {
                    "ok": False,
                    "stage": "cut-apply",
                    "error": f"删除索引越界: {sorted(bad)} 不在 [0,{n - 1}]（0-based）",
                    "cause": "index_out_of_range",
                },
                exit_code=2,
            )
            return

    # ⛔ 红线第一条（此前**只写在提示词里**，内核不拦 —— 这次补上）：
    #    幻觉嫌疑段（时长 ≤0.5s 却 ≥6 字）在音频上其实是**一整句连贯的话**，
    #    删它的时间轴会把说话声切断，这是历史效果差的根因。
    #    判定复用 common.is_suspect_hallucination（与 plan 同一套规则，不漂移）。
    #    真遇到"确实该删"的例外，必须由主管显式传 --allow-hallucination-cut ——
    #    把例外做成一次**有记录的决定**，而不是一个静默的默认行为。
    hallucination_hits = sorted(d for d in drop if is_suspect_hallucination(segs[d]))
    if hallucination_hits and not allow_hallucination_cut:
        emit({
            "ok": False,
            "stage": "cut-apply",
            "cause": "hallucination_cut_forbidden",
            "error": f"删除集里含 {len(hallucination_hits)} 个幻觉嫌疑段: {hallucination_hits}",
            "hint": "这些段时长 ≤0.5s 却 ≥6 字（Whisper 累积幻觉），音频上是一整句连贯的话，"
                    "删时间轴会把说话声切断 —— 它们只在字幕层合并（stage3 自动处理）。"
                    "请把它们从 drop_segment_ids 里移除。确需删除须由主管显式传 "
                    "args:[\"--allow-hallucination-cut\"]（会记录本次决定）。",
            "segments": [
                {"idx": d, "start": segs[d].get("start"), "end": segs[d].get("end"),
                 "text": (segs[d].get("text") or "").strip()}
                for d in hallucination_hits[:20]
            ],
        }, exit_code=2)
        return

    keep = [i for i in range(n) if i not in drop]
    if not keep:
        emit(
            {"ok": False, "stage": "cut-apply", "error": "删除集覆盖了全部片段",
             "cause": "empty_keep_set"},
            exit_code=2,
        )
        return

    # 1) 逐字稿（保留段，每段一行，套用 text_fixes）
    lines = [text_fixes.get(i, (segs[i].get("text") or "").strip()) for i in keep]
    project.edited_transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 2) 保留块（skill：索引连续且 gap<=5s 才合并）
    blocks_idx: list[list[int]] = [[keep[0]]]
    for k in keep[1:]:
        prev = blocks_idx[-1][-1]
        gap = segs[k]["start"] - segs[prev]["end"]
        if k == prev + 1 and gap <= GAP_MERGE_SECONDS:
            blocks_idx[-1].append(k)
        else:
            blocks_idx.append([k])

    blocks, cum = [], 0.0
    for b in blocks_idx:
        orig_s = round(segs[b[0]]["start"], 3)
        orig_e = round(segs[b[-1]]["end"], 3)
        dur = orig_e - orig_s
        blocks.append(
            {
                "edit_start": round(cum, 3),
                "edit_end": round(cum + dur, 3),
                "orig_start": orig_s,
                "orig_end": orig_e,
                "n_segs": len(b),
                "first_seg": b[0] + 1,   # 1-based，供阶段3 反查
                "last_seg": b[-1] + 1,
            }
        )
        cum += dur
    write_json(project.cut_blocks, blocks)

    # 3) 剪后时间轴 —— **剪辑的产物**，由剪裁策划在剪切完成时立即建立。
    #    字幕与特效都消费这一份（特效因此不必再等字幕阶段）。
    timeline, tl_stats = build_timeline(segs, blocks)
    write_json(project.timeline_map, {
        "source_duration": round(segs[-1].get("end", 0.0), 3) if segs else None,
        "edit_duration": round(cum, 3),
        "segment_count": len(timeline),
        # ⚠️ 契约说"时间轴条数 == 保留段数"，但空文本/缺时间戳/越界的段会被跳过。
        #    以前这些跳过是**静默**的（只在数目上体现，看不出原因），现在显式落盘。
        "stats": tl_stats,
        "note": (
            "以下时间全部是**剪辑后**时间轴（秒）。字幕与特效都消费这一份，"
            "不要用 edit/transcript.json 的时间 —— 那是剪辑前的，每过一个剪点就累积偏移。"
        ),
        "segments": timeline,
    })

    # 4) FFmpeg 裁切脚本（trim+concat 重编码，保证音画同步）
    src_flag = str(decisions.get("source") or "input/raw.mp4")
    src_path = (project.root / src_flag).resolve()
    out_path = project.edited_video.resolve().as_posix()
    out_path_rel = project.edited_video.relative_to(project.root).as_posix()

    parts, vlab, alab = [], [], []
    for i, b in enumerate(blocks):
        s, e = b["orig_start"], b["orig_end"]
        parts.append(
            f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]"
        )
        vlab.append(f"[v{i}]")
        alab.append(f"[a{i}]")
    concat_in = "".join(f"{v}{a}" for v, a in zip(vlab, alab))
    fc = ";".join(parts) + f";{concat_in}concat=n={len(blocks)}:v=1:a=1[outv][outa]"

    filter_file = project.edit_dir / "filter_cut.txt"
    filter_file.write_text(fc, encoding="utf-8", newline="\n")

    def _rel(p: Path) -> str:
        """把路径转成**相对 `edit/` 的 POSIX 形式**（`run_cut.sh` 的 cwd 就是 `edit/`）。

        ⚠️ 为什么不写绝对路径：真跑撞到过（2026-10-05）—— 绝对路径里的**非 ASCII 字符**
        （`.../AI学习/...`）经 Git Bash → Windows ffmpeg 的 argv 传递后编码被破坏，
        ffmpeg 报 `Error opening file .../filter_cut.txt` / `Invalid argument`，
        于是**只要项目路径含中文，cut-apply 就跑不通** —— 而本仓库恰好就在 `AI学习` 下。
        改成相对路径后，bash 与 ffmpeg 只看到纯 ASCII 的名字，真实路径由 Windows 自己解析。

        跨盘符时 `relpath` 会抛 `ValueError`（此时只能退回绝对路径，属已知限制）。
        """
        try:
            return Path(os.path.relpath(p, project.edit_dir)).as_posix()
        except ValueError:  # 不同盘符
            return p.as_posix()

    cmd = (
        f'ffmpeg -y -i "{_rel(src_path)}" '
        f'-filter_complex_script "{_rel(filter_file)}" '
        f'-map "[outv]" -map "[outa]" '
        f"-c:v libx264 -preset {PRESET_CUT} -crf {CRF_CUT} -pix_fmt yuv420p "
        f'-c:a aac -b:a {AUDIO_BITRATE} -avoid_negative_ts make_zero "{_rel(out_path)}"'
    )
    runner = project.edit_dir / "run_cut.sh"
    runner.write_text(cmd + "\n", encoding="utf-8", newline="\n")

    new_duration = round(sum(b["edit_end"] - b["edit_start"] for b in blocks), 3)
    report = {
        "stage": "cut-apply",
        "source": src_flag,
        "source_duration": round(segs[-1]["end"], 3),
        "dropped_segments": len(drop),
        "kept_segments": len(keep),
        "blocks": len(blocks),
        "new_duration": new_duration,
        "text_fixes": len(text_fixes),
        # CP1 复核用：把"删了但仍是幻觉嫌疑"的段显式列出来（正常应为空）
        "hallucination_cut": hallucination_hits,
        "hallucination_cut_allowed": bool(allow_hallucination_cut),
        # 剪后时间轴计数（契约：应等于保留段数；不等时这里能看到原因）
        "timeline": tl_stats,
        "edited_transcript": str(project.edited_transcript),
        "cut_blocks": str(project.cut_blocks),
        "timeline_map": str(project.timeline_map),
        "run_script": str(runner),
        "executed": False,
    }

    if do_run:
        run_bash_script(runner)
        report["executed"] = True
        report["edited_video"] = out_path_rel

    write_json(project.cut_report, report)
    emit({"ok": True, **report, "next": "srt" if do_run else "cut-apply --run"})


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段2 剪裁")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="产出 plan_context.json（证据）")
    p_plan.add_argument("--project", required=True)
    p_apply = sub.add_parser("apply", help="按 edit_decisions.json 落地并断言")
    p_apply.add_argument("--project", required=True)
    p_apply.add_argument("--run", action="store_true", help="立即执行 FFmpeg 裁切")
    p_apply.add_argument(
        "--allow-hallucination-cut", action="store_true",
        help="⛔ 默认拒绝删除幻觉嫌疑段（时长≤0.5s 却≥6字，只该在字幕层合并）；"
             "确需删除时由主管显式开启，并会被记进 cut_report.json")
    args = ap.parse_args()

    project = Project.resolve(args.project)
    project.ensure_dirs()
    if args.cmd == "plan":
        guarded(cmd_plan, project)
    else:
        guarded(cmd_apply, project, do_run=args.run,
                allow_hallucination_cut=args.allow_hallucination_cut)


if __name__ == "__main__":
    main()
