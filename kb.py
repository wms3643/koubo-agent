#!/usr/bin/env python3
"""kb.py —— 口播剪辑流水线统一入口（确定性层 CLI）

所有 stage 都是**纯确定性**的：给定输入产出固定输出并自检，不做主观判断。
DSH 侧插件只需 shell 出本入口，因此插件层零业务逻辑、工具 schema 恒定。

用法：
    python kb.py status          --project <dir>
    python kb.py transcribe      --project <dir> [--source x.mp4]
    python kb.py cut-plan        --project <dir>
    python kb.py cut-apply       --project <dir> [--run]
    python kb.py srt             --project <dir>
    python kb.py burn-test       --project <dir>
    python kb.py burn-full       --project <dir>
    python kb.py qc              --project <dir> [--target final] [--frames-at 1,5,9]
    python kb.py assemble        --project <dir> --spec edit/assemble.json
    python kb.py mix             --project <dir> --spec edit/mix.json
    python kb.py bgm-generate    --project <dir> [--spec edit/bgm.json] [--dry-run]
    python kb.py overlay-render  --project <dir> [--output overlay.mov]

退出码：0 = 通过；2 = 业务性失败（带 cause，可路由回退）；1 = 环境/异常错误。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline.common import (  # noqa: E402
    Project, guarded, load_segments, probe, read_json,
)

PIPELINE = ROOT / "pipeline"

# cmd -> (stage 脚本, 传给脚本的子命令或 None)
DISPATCH: dict[str, tuple[str, str | None]] = {
    "transcribe":    ("stage1_transcribe.py", None),
    "cut-plan":      ("stage2_cut.py",        "plan"),
    "cut-apply":     ("stage2_cut.py",        "apply"),
    "srt":           ("stage3_srt.py",        None),
    "burn-test":     ("stage4_burn.py",       "burn-test"),
    "burn-full":     ("stage4_burn.py",       "burn-full"),
    "assemble":      ("stage5_assemble.py",   "assemble"),
    "mix":           ("stage5_assemble.py",   "mix"),
    "bgm-generate":  ("stage7_bgm.py",        None),
    "qc":            ("stage6_qc.py",         None),
}


# ---------------------------------------------------------------------------
#  status —— 让 agent 一眼看清项目停在哪一步（断点续跑的依据）
# ---------------------------------------------------------------------------
def cmd_status(project: Project) -> int:
    artifacts = {
        "raw_video": project.raw_video,
        "transcript_json": project.transcript_json,
        "plan_context": project.plan_context,
        "edit_decisions": project.edit_decisions,
        "cut_blocks": project.cut_blocks,
        "edited_video": project.edited_video,
        "final_srt": project.final_srt,
        "subtitle_test_video": project.subtitle_test_video,
        "subtitled_full": project.renders_dir / "subtitled_full.mp4",
        "final_video": project.final_video,
        "bgm_audio": next((p for p in sorted(project.bgm_dir.glob("bgm.*")) if p.is_file()),
                          project.bgm_dir / "bgm.mp3"),
        "qc_report": project.qc_report,
    }
    present = {k: (v.is_file(), str(v)) for k, v in artifacts.items()}

    # 动效层：契约要求"透明叠加层不得默认跳过"，所以它必须是一个**可断点判定**的阶段。
    # ⚠️ 这里之前**没有 overlay 阶段**，后果是真跑撞到过的：用 `--no-overlay` 跑过一遍后，
    #    哪怕 overlays/renders 下一个 MOV 都没有，status 也会说 `done` ⇒ 之后每次续跑
    #    都直接交付那份**没有动效层**的成片，而"缺动效"这件事在断点判定里根本不存在。
    overlay_movs = sorted(p for p in (project.overlays_dir / "renders").glob("*.mov")
                          if p.is_file())

    stages = {
        "1_transcribe": present["transcript_json"][0],
        "2_plan": present["plan_context"][0],
        "2_decide": present["edit_decisions"][0],
        "2_apply": present["edited_video"][0],
        "3_srt": present["final_srt"][0],
        "4_burn": present["subtitled_full"][0] or present["final_video"][0],
        "5_overlay": bool(overlay_movs),
        "6_qc": present["qc_report"][0],
    }
    next_stage = next((k for k, done in stages.items() if not done), "done")

    info: dict = {"ok": True, "stage": "status", "project": str(project.root)}
    if present["transcript_json"][0]:
        segs = load_segments(project.transcript_json)
        info["segment_count"] = len(segs)
        info["source_duration"] = round(segs[-1]["end"], 3) if segs else 0.0
    for key, path in (("edited_spec", project.edited_video),
                      ("final_spec", project.final_video)):
        if path.is_file():
            info[key] = probe(path)
    if present["qc_report"][0]:
        rep = read_json(project.qc_report)
        info["last_qc_ok"] = rep.get("ok")
        info["last_qc_causes"] = rep.get("causes", [])

    info["overlay"] = {
        "index_html": str(project.overlays_dir / "index.html"),
        "index_html_exists": (project.overlays_dir / "index.html").is_file(),
        "rendered_movs": [str(p) for p in overlay_movs],
    }
    info["artifacts"] = {k: {"exists": v[0], "path": v[1]} for k, v in present.items()}
    info["stages_done"] = stages
    info["next_stage"] = next_stage
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------------------
#  overlay-render —— HyperFrames 透明叠加层渲染（skill 阶段4）
# ---------------------------------------------------------------------------
def cmd_overlay_render(project: Project, output: str) -> int:
    """渲染 <project>/overlays 下的 HyperFrames 工程。

    ⛔ skill 红线：全片透明叠加层必须 `--format mov`（ProRes4444 带 alpha），
    根元素必须带 `data-composition-id="main" data-width data-height`。
    """
    proj_dir = project.overlays_dir
    if not (proj_dir / "index.html").is_file():
        print(json.dumps(
            {"ok": False, "cause": "missing_overlay_project",
             "error": f"{proj_dir}/index.html 不存在（动效子代理需先产出工程）"},
            ensure_ascii=False, indent=1))
        return 2

    bin_name = "hyperframes.cmd" if sys.platform == "win32" else "hyperframes"
    binary = proj_dir / "node_modules" / ".bin" / bin_name
    if not binary.is_file():
        print(json.dumps(
            {"ok": False, "cause": "missing_hyperframes",
             "error": f"未找到 {binary}；先在 {proj_dir} 执行 "
                      f"`npm install hyperframes@0.7.94`"},
            ensure_ascii=False, indent=1))
        return 2

    out_path = proj_dir / "renders" / output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ① 先过 hyperframes 自带的静态闸门（秒级）。
    # ⚠️ 为什么必须在渲染前挡：下面这些都是**渲染不会报错**的问题 ——
    #    · 缺 `window.__timelines` 注册 ⇒ 动画根本不播，产出静止画面；
    #    · 根元素缺 `data-start` / 缺时长来源 ⇒ "Composition has zero duration"；
    #    · 初始隐藏态写成 `tl.set(...,0)` ⇒ 第 0 帧露出未隐藏状态（开头闪一下）。
    #    渲染一次是分钟级（ProRes4444），lint 是秒级；更重要的是，
    #    没有这道闸门时这些问题会**安静地**进到成片里，而 agent 拿不到可行动的报错。
    lint = subprocess.run([str(binary), "lint"], cwd=str(proj_dir), text=True,
                          encoding="utf-8", errors="replace", capture_output=True)
    if lint.returncode != 0:
        print(json.dumps(
            {"ok": False, "cause": "hyperframes_lint_failed",
             "error": (lint.stdout or lint.stderr or "")[-1500:],
             "hint": "按上面每条 Fix 改 overlays/index.html 后重跑 overlay-render"},
            ensure_ascii=False, indent=1))
        return 2

    # ⚠️ 传给 hyperframes 的必须是**相对 cwd（= overlays/）的路径**，不能只给文件名。
    #    它的 `--output` 以自身 cwd 为基准（默认值本身就是 `renders/<name>.mp4`），
    #    只给 `overlay.mov` 会写进 `overlays/overlay.mov`，而内核随后去
    #    `overlays/renders/overlay.mov` 取 —— 真跑撞到过：**报 ok=true，产物却在别处**。
    rel_out = f"renders/{output}"
    cmd = [
        str(binary), "render", ".", "-c", "index.html",
        "--format", "mov", "--quality", "standard", "--fps", "30",
        "--no-experimental-fast-capture", "--output", rel_out,
    ]
    proc = subprocess.run(cmd, cwd=str(proj_dir), text=True,
                          encoding="utf-8", errors="replace", capture_output=True)
    if proc.returncode != 0:
        print(json.dumps(
            {"ok": False, "cause": "hyperframes_render_failed",
             "error": (proc.stderr or proc.stdout or "")[-1200:]},
            ensure_ascii=False, indent=1))
        return 2

    # ⚠️ 退出码为 0 **不足以**说明成功：必须确认产物真的存在且非空。
    #    缺这道校验的后果与 stage4/stage5 里硬写 `"ok": True` 是同一类 ——
    #    报告说成功、实际没产出，而下游（assemble）会去找一个不存在的 MOV。
    if not out_path.is_file() or out_path.stat().st_size == 0:
        print(json.dumps(
            {"ok": False, "cause": "hyperframes_render_failed",
             "error": (f"hyperframes 退出码 0，但产物不存在或为空：{out_path}；"
                       f"实际写入路径可能与 --output 的基准目录不一致"
                       f"（期望 renders/{output}）"),
             "stdout_tail": (proc.stdout or "")[-800:],
             "stderr_tail": (proc.stderr or "")[-800:]},
            ensure_ascii=False, indent=1))
        return 2

    print(json.dumps({
        "ok": True, "stage": "overlay-render", "output": str(out_path),
        "bytes": out_path.stat().st_size,
        "format": "mov/ProRes4444", "next": "assemble",
    }, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd, rest = sys.argv[1], sys.argv[2:]

    if cmd in ("status", "overlay-render"):
        if "--project" not in rest:
            print("需要 --project")
            return 1
        project = Project.resolve(rest[rest.index("--project") + 1])
        if cmd == "status":
            return cmd_status(project)
        output = rest[rest.index("--output") + 1] if "--output" in rest else "overlay.mov"
        return cmd_overlay_render(project, output)

    if cmd not in DISPATCH:
        print(f"未知子命令: {cmd}\n{__doc__}")
        return 1

    script, subcmd = DISPATCH[cmd]
    argv = [sys.executable, str(PIPELINE / script)]
    if subcmd:
        argv.append(subcmd)
    argv += rest
    return subprocess.run(argv, text=True, encoding="utf-8", errors="replace").returncode


if __name__ == "__main__":
    _rc = guarded(main)
    sys.exit(0 if _rc is None else _rc)
