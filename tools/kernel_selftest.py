"""内核回归自检：阶段3 的两个新行为（零 GPU、零 ffmpeg）。

覆盖：
    1. 报告里出现 `off_block_segments[]` 明细（跨删除区句子，供字幕子代理裁决）
    2. `edit/subtitle_fixes.json` 能被合并进 text_fixes 并真正改写 final.srt
    3. **写回通道不污染剪裁决策**：`edit_decisions.json` 的内容在字幕子代理写回后原样不变

用法：python tools/kernel_selftest.py [项目目录]
默认项目：projects/_smoke
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.common import Project, read_json, write_json  # noqa: E402
from pipeline.stage3_srt import build_srt  # noqa: E402

MARKER = "自检占位字符串ZZQ"


def synthetic_case(workdir: Path) -> list[str]:
    """合成最小项目，逼出"跨删除区句子"。

    真实素材里 dropped_off_block 常常是 0（删除区多在首尾），所以必须合成一个
    "保留段的 word 时间戳越界到删除区间"的案例，才能验证明细字段真的成形。
    """
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   | {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    prj = workdir / "_selftest_synth"
    for sub in ("input", "edit", "renders"):
        (prj / sub).mkdir(parents=True, exist_ok=True)

    # 段0 保留，但最后一个词的时间戳落到被删区间(4~8s) → 整句 remap 失败
    segs = [
        {"id": 0, "start": 0.0, "end": 4.0, "text": "第一句正常内容",
         "words": [{"word": "第一句", "start": 0.0, "end": 1.5},
                   {"word": "正常内容", "start": 1.5, "end": 3.0},
                   {"word": "越界词", "start": 5.0, "end": 6.0}]},
        {"id": 1, "start": 6.0, "end": 8.0, "text": "这段被删掉",
         "words": [{"word": "这段", "start": 6.0, "end": 7.0},
                   {"word": "被删掉", "start": 7.0, "end": 8.0}]},
        {"id": 2, "start": 8.0, "end": 10.0, "text": "第三句正常内容",
         "words": [{"word": "第三句", "start": 8.0, "end": 9.0},
                   {"word": "正常内容", "start": 9.0, "end": 10.0}]},
    ]
    blocks = [
        {"edit_start": 0.0, "edit_end": 4.0, "orig_start": 0.0, "orig_end": 4.0,
         "first_seg": 1, "last_seg": 1},
        {"edit_start": 4.0, "edit_end": 6.0, "orig_start": 8.0, "orig_end": 10.0,
         "first_seg": 3, "last_seg": 3},
    ]
    write_json(prj / "edit" / "transcript.json", segs)
    write_json(prj / "edit" / "cut_blocks.json", blocks)
    write_json(prj / "edit" / "edit_decisions.json",
               {"source": "synthetic", "drop_segment_ids": [1], "text_fixes": {}})

    rep = build_srt(Project.resolve(prj))
    check("合成: ok", rep.get("ok") is True)
    detail = rep.get("off_block_segments") or []
    check("合成: 明细非空", len(detail) == 1,
          f"dropped_off_block={rep.get('dropped_off_block')}")
    if detail:
        s = detail[0]
        check("合成: 文本正确", s.get("text") == "第一句正常内容", str(s.get("text")))
        check("合成: 原时间区间正确",
              abs(s.get("orig_start", -1) - 0.0) < 1e-6 and abs(s.get("orig_end", -1) - 6.0) < 1e-6,
              f"{s.get('orig_start')}~{s.get('orig_end')}")
    check("合成: 仅剩 1 条字幕", rep.get("subtitle_count") == 1,
          str(rep.get("subtitle_count")))
    return fails


def main() -> int:
    prj = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "projects" / "_smoke"
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   | {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    project = Project.resolve(prj)
    check("前置 transcript.json", project.transcript_json.is_file())
    check("前置 cut_blocks.json", project.cut_blocks.is_file())
    check("前置 edit_decisions.json", project.edit_decisions.is_file())
    if fails:
        print("\n前置缺失，终止")
        return 1

    decisions_before = read_json(project.edit_decisions)

    # ── 1a) 剪后时间轴：**由剪裁策划在剪切时建立**（不是字幕阶段的产物）──
    blocks = read_json(project.cut_blocks)
    p2 = subprocess.run(
        [sys.executable, str(ROOT / "kb.py"), "cut-apply", "--project", str(prj)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    check("剪切阶段（cut-apply）退出码 0", p2.returncode == 0, (p2.stderr or "")[-160:])
    check("剪切阶段即产出 timeline_map.json", project.timeline_map.is_file())
    if project.timeline_map.is_file():
        tm = read_json(project.timeline_map)
        ts = tm.get("segments") or []
        keep_n = sum(b["n_segs"] for b in blocks)
        check("时间轴条数 == 保留段数（取自 cut_blocks）", len(ts) == keep_n,
              f"{len(ts)} vs {keep_n}")
        if ts:
            starts = [x["edit_start"] for x in ts]
            check("时间轴单调不减",
                  all(starts[i] <= starts[i + 1] + 1e-6 for i in range(len(starts) - 1)))
            dur = tm.get("edit_duration") or 0

    # ── 1b) 字幕阶段：只消费时间轴，不产出 ──
    if project.subtitle_fixes.is_file():
        project.subtitle_fixes.unlink()
    base = build_srt(project)

    check("基线 ok", base.get("ok") is True)
    check("报告含 off_block_segments", "off_block_segments" in base,
          f"dropped_off_block={base.get('dropped_off_block')}")
    check("off_block_segments 是列表", isinstance(base.get("off_block_segments"), list))
    if base.get("off_block_segments"):
        s = base["off_block_segments"][0]
        check("off_block 明细字段齐全",
              all(k in s for k in ("text", "orig_start", "orig_end", "duration")),
              repr(s)[:120])
    check("text_fixes_applied 已暴露", "text_fixes_applied" in base,
          f"= {base.get('text_fixes_applied')}")
    check("字幕阶段只声明时间轴存在（不自产）", base.get("timeline_map_present") is True)

    if project.timeline_map.is_file():
        tm = read_json(project.timeline_map)
        ts = tm.get("segments") or []
        if ts:
            check("末段不越过片长", ts[-1]["edit_end"] <= dur + 1.0,
                  f"{ts[-1]['edit_end']} vs 片长 {dur}")
            ws = ts[0].get("words") or []
            check("词级时间嵌套在句区间内",
                  all(ts[0]["edit_start"] - 0.01 <= w["s"] <= w["e"] <= ts[0]["edit_end"] + 0.01
                      for w in ws),
                  f"首段 {len(ws)} 词")
            check("每条都带两套坐标（source_* / edit_*）",
                  all("source_start" in x and "edit_start" in x for x in ts))
            check("剪辑只前移不后移（source_start >= edit_start）",
                  all(x["source_start"] >= x["edit_start"] - 0.01 for x in ts),
                  f"首条 source={ts[0]['source_start']} / edit={ts[0]['edit_start']}")
    base_count = base.get("subtitle_count")
    base_fixes = base.get("text_fixes_applied")

    # ── 2) 字幕子代理写回通道 ──
    blocks = read_json(project.cut_blocks)
    keep_idx = blocks[0]["first_seg"] - 1          # 第一个保留段（0-based）
    write_json(project.subtitle_fixes, {"text_fixes": {str(keep_idx): MARKER}})

    after = build_srt(project)
    check("写回后仍 ok", after.get("ok") is True)
    check("text_fixes_applied +1",
          after.get("text_fixes_applied") == (base_fixes or 0) + 1,
          f"{base_fixes} -> {after.get('text_fixes_applied')}")
    check("字幕条数未因写回而变", after.get("subtitle_count") == base_count,
          f"{base_count} -> {after.get('subtitle_count')}")

    srt_text = project.final_srt.read_text(encoding="utf-8")
    check("final.srt 里出现改写后的文本", MARKER in srt_text)
    check("final.srt 仍无 BOM",
          not project.final_srt.read_bytes().startswith(b"\xef\xbb\xbf"))
    check("写回后三项自检仍全过", all(after.get("checks", {}).values()),
          str(after.get("checks")))

    # ── 3) 不污染剪裁决策 ──
    decisions_after = read_json(project.edit_decisions)
    check("edit_decisions.json 未被字幕写回改动", decisions_after == decisions_before)

    # ── 4) 合成用例：逼出"跨删除区句子"，验证明细字段成形 ──
    fails += synthetic_case(ROOT / "projects")

    # ── 清理本次测试写入的残留，避免污染后续观察 ──
    if project.subtitle_fixes.is_file():
        project.subtitle_fixes.unlink()

    print(f"\n{'=' * 46}")
    print(f"{'全部通过' if not fails else f'{len(fails)} 项失败'}")
    if fails:
        for f in fails:
            print(f"  - {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
