"""内核「护栏与断言」回归测试（零模型、零 GPU、零真钱；仅需 ffmpeg）。

为什么单独一个文件：
    `kernel_selftest.py` 覆盖的是**阶段3 的字段成形**；本文件覆盖的是**红线与失败路由** ——
    也就是"错了必须能被拦下、且拦下时要给出可路由的 cause"这一类。它们是 2026-10-05
    代码审查里查出的**断言漏洞**，每一个都曾是"文档写了、代码里没有"或"看起来在跑、其实没跑"。

覆盖：
    A 幻觉段护栏      —— cut-apply 删幻觉嫌疑段必须拒绝（cause=hallucination_cut_forbidden）
    B 决策文件写错    —— 非法 drop/text_fixes 键给 bad_spec（退出码 2），不再崩成 kernel_exception
    B3 BOM 容忍       —— 带 BOM 的 edit_decisions.json 仍可读
    C 越界            —— index_out_of_range
    D 质检作用域      —— qc(subtitle_test) 不得拿全片 SRT 去比 10s 测试片（假 cause）
    D2 无音频流        —— 必须报 missing_stream（退出码 2），不得崩成 kernel_exception（退出码 1）
    E assemble 干跑   —— --dry-run 真不产文件；转场超过段长给 bad_spec
    F mix 素材缺失    —— bgm_missing（可路由），而不是 ffmpeg 的 exit=1
    G 付费三项        —— 查价失败拒绝提交 / data 包装也能读到价 / 超预算不提交

用法：python tools/kernel_guard_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.common import FFMPEG, write_json  # noqa: E402

WORK = ROOT / "projects" / "_guard_regress"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   | {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------
def kb(*argv: str, env: dict | None = None, timeout: int = 180) -> tuple[int, dict]:
    """跑真 CLI，返回 (退出码, 解析出的 JSON)。"""
    p = subprocess.run(
        [sys.executable, str(ROOT / "kb.py"), *argv],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        env=env, timeout=timeout,
    )
    out = (p.stdout or "").strip()
    try:
        obj = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        obj = {"_raw": out[-400:], "_stderr": (p.stderr or "")[-400:]}
    return p.returncode, obj


def project(name: str) -> Path:
    prj = WORK / name
    for sub in ("input", "edit", "renders", "edit/tmpimg"):
        (prj / sub).mkdir(parents=True, exist_ok=True)
    return prj


def make_video(path: Path, seconds: float, *, with_audio: bool = True,
               freq: int = 220, fps: int = 15, size: str = "160x90") -> None:
    cmd = [FFMPEG, "-y", "-v", "error",
           "-f", "lavfi", "-i", f"color=c=gray:s={size}:r={fps}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    else:
        cmd += ["-an", "-t", str(seconds)]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def seg(idx: int, start: float, end: float, text: str) -> dict:
    """造一个带词级时间戳的段（词平分区间）。"""
    words = [{"start": start, "end": end, "word": text, "probability": 0.9}]
    return {"id": idx, "start": start, "end": end, "text": text, "words": words}


def transcript_for(prj: Path, segs: list[dict]) -> None:
    write_json(prj / "edit" / "transcript.json", segs)


# 一个"正常"段 + 一个"幻觉嫌疑"段（时长 0.3s 却有 8 个字：物理上说不完）
def _normal_segs() -> list[dict]:
    return [
        seg(0, 0.0, 4.0, "第一句正常内容"),      # 8 字 / 4.0s → 正常
        seg(1, 4.0, 4.3, "这是一句幻觉嫌疑段"),  # 9 字 / 0.3s → 幻觉嫌疑
        seg(2, 4.3, 8.0, "第二句正常内容"),
    ]


# ---------------------------------------------------------------------------
#  A/B/C — cut-apply 的护栏与失败路由（不起 ffmpeg）
# ---------------------------------------------------------------------------
def group_apply() -> None:
    print("\n=== A 幻觉段护栏 ===")
    prj = project("hallu")
    transcript_for(prj, _normal_segs())

    # A1 试图删掉幻觉嫌疑段（idx=1）→ 必须拒绝
    write_json(prj / "edit" / "edit_decisions.json",
               {"source": "input/raw.mp4", "drop_segment_ids": [1], "text_fixes": {}})
    rc, obj = kb("cut-apply", "--project", str(prj))
    check("A1 删幻觉段被拒（exit=2）", rc == 2, f"rc={rc}")
    check("A1 cause=hallucination_cut_forbidden",
          obj.get("cause") == "hallucination_cut_forbidden", f"{obj.get('cause')}")
    check("A1 回带被拒段明细", bool(obj.get("segments")),
          str(obj.get("segments"))[:100])

    # A2 显式放行 → 通过，并在报告里留痕
    rc, obj = kb("cut-apply", "--project", str(prj), "--allow-hallucination-cut")
    check("A2 显式放行后通过（exit=0）", rc == 0, f"rc={rc} {obj.get('cause')}")
    check("A2 报告记录已放行", obj.get("hallucination_cut_allowed") is True)
    check("A2 报告列出被删的幻觉段", obj.get("hallucination_cut") == [1],
          str(obj.get("hallucination_cut")))

    # A3 删正常段 → 通过
    write_json(prj / "edit" / "edit_decisions.json",
               {"source": "input/raw.mp4", "drop_segment_ids": [0], "text_fixes": {}})
    rc, obj = kb("cut-apply", "--project", str(prj))
    check("A3 删正常段通过（exit=0）", rc == 0, f"rc={rc} {obj.get('cause')}")
    check("A3 报告带时间轴计数", isinstance(obj.get("timeline"), dict),
          str(obj.get("timeline")))
    tl = obj.get("timeline") or {}
    check("A3 时间轴条数 == 保留段数",
          tl.get("timeline_segments") == tl.get("keep_segments"),
          f"{tl.get('timeline_segments')} vs {tl.get('keep_segments')}")

    print("\n=== B 决策文件写错 → bad_spec（可路由，而不是 exit=1）===")
    prj = project("badspec")
    transcript_for(prj, _normal_segs())

    write_json(prj / "edit" / "edit_decisions.json",
               {"drop_segment_ids": ["x"], "text_fixes": {}})
    rc, obj = kb("cut-apply", "--project", str(prj))
    check("B1 非法 drop 元素 → exit=2 cause=bad_spec",
          rc == 2 and obj.get("cause") == "bad_spec", f"rc={rc} cause={obj.get('cause')}")

    write_json(prj / "edit" / "edit_decisions.json",
               {"drop_segment_ids": [0], "text_fixes": {"idx-1": "改写"}})
    rc, obj = kb("cut-apply", "--project", str(prj))
    check("B2 非法 text_fixes 键 → exit=2 cause=bad_spec",
          rc == 2 and obj.get("cause") == "bad_spec", f"rc={rc} cause={obj.get('cause')}")

    print("\n=== B3 决策文件带 BOM 仍可读（Windows 编辑器常见）===")
    prj = project("bom")
    transcript_for(prj, _normal_segs())
    (prj / "edit" / "edit_decisions.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(
            {"source": "input/raw.mp4", "drop_segment_ids": [2], "text_fixes": {}},
            ensure_ascii=False).encode("utf-8"))
    rc, obj = kb("cut-apply", "--project", str(prj))
    check("B3 BOM 决策文件通过（exit=0）", rc == 0, f"rc={rc} cause={obj.get('cause')}")

    print("\n=== C 越界 ===")
    prj = project("oob")
    transcript_for(prj, _normal_segs())
    write_json(prj / "edit" / "edit_decisions.json",
               {"drop_segment_ids": [999], "text_fixes": {}})
    rc, obj = kb("cut-apply", "--project", str(prj))
    check("C1 越界 → exit=2 cause=index_out_of_range",
          rc == 2 and obj.get("cause") == "index_out_of_range",
          f"rc={rc} cause={obj.get('cause')}")


# ---------------------------------------------------------------------------
#  D — 质检作用域与无音频流
# ---------------------------------------------------------------------------
def _build_qc_project(prj: Path) -> None:
    transcript_for(prj, [
        seg(0, 0.0, 4.0, "第一句正常内容"),
        seg(1, 4.0, 8.0, "第二句正常内容"),
        seg(2, 8.0, 12.0, "第三句正常内容"),
    ])
    write_json(prj / "edit" / "edit_decisions.json",
               {"source": "input/raw.mp4", "drop_segment_ids": [], "text_fixes": {}})
    make_video(prj / "renders" / "edited_final.mp4", 12.0)
    make_video(prj / "renders" / "subtitle_test_10s.mp4", 10.0)


def group_qc() -> None:
    print("\n=== D 质检作用域：subtitle_test 不得拿全片 SRT 去比 10s 测试片 ===")
    prj = project("qc")
    _build_qc_project(prj)
    rc, _ = kb("cut-apply", "--project", str(prj))
    check("D 前置 cut-apply 通过", rc == 0)
    rc, obj = kb("srt", "--project", str(prj))
    check("D 前置 srt 通过", rc == 0, f"rc={rc} {obj.get('cause')}")
    check("D 前置 final.srt 覆盖全片", (obj.get("subtitle_count") or 0) >= 3,
          str(obj.get("subtitle_count")))

    rc, obj = kb("qc", "--project", str(prj), "--target", "subtitle_test")
    ids = {c["id"] for c in (obj.get("checks") or [])}
    check("D1 subtitle_test 不做「字幕与时长吻合」检查",
          "srt_timeline_matches_video" not in ids, str(sorted(ids)))
    check("D1 subtitle_test 不报 srt_timeline_mismatch",
          "srt_timeline_mismatch" not in (obj.get("causes") or []),
          str(obj.get("causes")))
    check("D1 subtitle_test 不做「实测时长 vs 保留块计划」检查",
          "edited_duration_vs_plan" not in ids)

    rc, obj = kb("qc", "--project", str(prj), "--target", "edited")
    ids = {c["id"] for c in (obj.get("checks") or [])}
    check("D2 edited 目标**要**做这两项检查",
          {"srt_timeline_matches_video", "edited_duration_vs_plan"} <= ids, str(sorted(ids)))
    check("D2 edited 目标全绿（exit=0）", rc == 0, f"rc={rc} causes={obj.get('causes')}")
    check("D2 av_duration_equal 恒在场", "av_duration_equal" in ids)

    print("\n=== D3 无音频流 → missing_stream（可路由），不得崩成 kernel_exception ===")
    prj2 = project("noaudio")
    make_video(prj2 / "renders" / "edited_final.mp4", 4.0, with_audio=False)
    rc, obj = kb("qc", "--project", str(prj2), "--target", "edited")
    check("D3 无音频 exit=2（不是 1）", rc == 2, f"rc={rc}")
    check("D3 cause 含 missing_stream",
          "missing_stream" in (obj.get("causes") or []), str(obj.get("causes")))
    check("D3 未退化成 kernel_exception", obj.get("cause") != "kernel_exception")
    check("D3 仍产出了结构化报告", isinstance(obj.get("checks"), list) and len(obj["checks"]) > 0)


# ---------------------------------------------------------------------------
#  E — assemble：--dry-run 与 bad_spec
# ---------------------------------------------------------------------------
def group_assemble() -> None:
    print("\n=== E assemble 干跑与规格校验 ===")
    prj = project("assemble")
    make_video(prj / "renders" / "base.mp4", 5.0)
    out = prj / "renders" / "out.mp4"
    write_json(prj / "edit" / "assemble.json",
               {"segments": [{"path": "renders/base.mp4"}],
                "output": "renders/out.mp4"})

    rc, obj = kb("assemble", "--project", str(prj), "--spec", "edit/assemble.json", "--dry-run")
    check("E1 干跑 exit=0", rc == 0, f"rc={rc} {obj.get('cause')}")
    check("E1 干跑标了 dry_run", obj.get("dry_run") is True)
    check("E1 干跑**不产出文件**", not out.is_file())
    check("E1 干跑给出预测时长", abs((obj.get("predicted_duration") or 0) - 5.0) < 0.3,
          str(obj.get("predicted_duration")))

    # 转场比段长还长 → 不可能缝接 ⇒ 必须是可路由的 bad_spec，而不是 exit=1
    write_json(prj / "edit" / "assemble_bad.json",
               {"segments": [{"path": "renders/base.mp4"}, {"path": "renders/base.mp4"}],
                "transition": 8.0, "output": "renders/bad.mp4"})
    rc, obj = kb("assemble", "--project", str(prj), "--spec", "edit/assemble_bad.json", "--dry-run")
    check("E2 转场超过段长 → exit=2 cause=bad_spec",
          rc == 2 and obj.get("cause") == "bad_spec", f"rc={rc} cause={obj.get('cause')}")
    check("E2 干跑失败也不产文件", not (prj / "renders" / "bad.mp4").is_file())

    print("\n=== F mix 素材缺失 → bgm_missing（可路由）===")
    prj2 = project("mix")
    make_video(prj2 / "input" / "voice.mp4", 3.0)
    write_json(prj2 / "edit" / "mix.json",
               {"video": "input/voice.mp4", "bgm": "input/not_here.wav",
                "output": "renders/final.mp4"})
    rc, obj = kb("mix", "--project", str(prj2), "--spec", "edit/mix.json")
    check("F1 BGM 缺失 → exit=2 cause=bgm_missing",
          rc == 2 and obj.get("cause") == "bgm_missing", f"rc={rc} cause={obj.get('cause')}")

    write_json(prj2 / "edit" / "mix_bad.json", {"video": "input/voice.mp4"})
    rc, obj = kb("mix", "--project", str(prj2), "--spec", "edit/mix_bad.json")
    check("F2 mix 规格缺 bgm 字段 → exit=2 cause=bad_spec",
          rc == 2 and obj.get("cause") == "bad_spec", f"rc={rc} cause={obj.get('cause')}")


# ---------------------------------------------------------------------------
#  G — 付费三项（本地桩，零真钱）
# ---------------------------------------------------------------------------
STATE: dict = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0].endswith("/pricing"):
            if STATE.get("pricing_broken"):
                return self._json(500, {"error": "stub: pricing unavailable"})
            groups = [{"group_name": "MM", "is_active": True, "in_key_whitelist": True,
                       "base_price": STATE.get("price", 0.4896),
                       "min_price": STATE.get("price", 0.4896)}]
            if STATE.get("pricing_wrapped"):
                # 忠实复现网关的 data 包装形态
                return self._json(200, {"code": 200, "data": {"channel_groups": groups}})
            return self._json(200, {"channel_groups": groups})
        self._json(404, {"error": "stub"})

    def do_POST(self):
        if self.path.endswith("/media/generate"):
            STATE["posts"] = STATE.get("posts", 0) + 1
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._json(200, {"code": 200, "data": {"task_id": 1, "state": "running",
                                                          "is_final": False}})
        self._json(404, {"error": "stub"})


def group_bgm() -> None:
    print("\n=== G 付费闸门（本地桩，不花真钱）===")
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    prj = project("bgmprice")
    write_json(prj / "edit" / "bgm.json",
               {"style": "Instrumental only, no vocals, calm restrained underscore, "
                         "loop-friendly, sits behind narration.",
                "output": "bgm/bgm.mp3"})
    env = {**os.environ, "LK_BASE_URL": base + "/v1", "LK_API_KEY": "sk-regress-not-real"}
    env.pop("LK_API_KEY_FILE", None)

    # G1 查价失败 → 干跑要如实说"没查到价"，真跑必须拒绝提交且一次都不 POST
    STATE.clear(); STATE["pricing_broken"] = True
    rc, obj = kb("bgm-generate", "--project", str(prj), "--dry-run", env=env)
    check("G1 查价失败时干跑 exit=0", rc == 0, f"rc={rc}")
    check("G1 干跑如实报 price_available=false", obj.get("price_available") is False,
          str(obj.get("price_available")))

    rc, obj = kb("bgm-generate", "--project", str(prj), "--confirm-paid", env=env)
    check("G1 查价失败 → 拒绝提交（exit=2 cause=bgm_pricing_unavailable）",
          rc == 2 and obj.get("cause") == "bgm_pricing_unavailable",
          f"rc={rc} cause={obj.get('cause')}")
    check("G1 **一次都没有 POST**（防在不知道单价时花钱）",
          STATE.get("posts", 0) == 0, f"posts={STATE.get('posts', 0)}")

    # G2 价格被 `data` 包装（网关真实形态）→ 仍必须读到价
    STATE.clear(); STATE["pricing_wrapped"] = True; STATE["price"] = 0.4896
    rc, obj = kb("bgm-generate", "--project", str(prj), "--dry-run", env=env)
    check("G2 `data` 包装下仍读到价格", obj.get("price_per_call") == 0.4896,
          str(obj.get("price_per_call")))

    # G2b 同一形态下超预算 → 必须拦
    STATE["price"] = 50.0
    rc, obj = kb("bgm-generate", "--project", str(prj), "--max-price", "1",
                 "--confirm-paid", env=env)
    check("G2b 超预算 → bgm_price_above_budget",
          rc == 2 and obj.get("cause") == "bgm_price_above_budget",
          f"rc={rc} cause={obj.get('cause')}")
    check("G2b 超预算未提交", STATE.get("posts", 0) == 0)

    # G3 付费闸门：价格正常，但没确认 ⇒ 绝不提交
    STATE.clear(); STATE["price"] = 0.4896
    rc, obj = kb("bgm-generate", "--project", str(prj), env=env)
    check("G3 未确认 → bgm_needs_confirm",
          rc == 2 and obj.get("cause") == "bgm_needs_confirm",
          f"rc={rc} cause={obj.get('cause')}")
    check("G3 未确认未提交", STATE.get("posts", 0) == 0)

    # G4 已有成品 ⇒ 连"确认"都不用谈，直接 bgm_exists（免费结论排在付费确认之前）
    (prj / "bgm").mkdir(parents=True, exist_ok=True)
    (prj / "bgm" / "bgm.mp3").write_bytes(b"\xff\xfb" + b"\x00" * 40_000)
    STATE.clear(); STATE["price"] = 0.4896
    rc, obj = kb("bgm-generate", "--project", str(prj), env=env)
    check("G4 已有成品 → bgm_exists（不先问付费）",
          rc == 2 and obj.get("cause") == "bgm_exists", f"rc={rc} cause={obj.get('cause')}")
    check("G4 未提交", STATE.get("posts", 0) == 0)

    srv.shutdown()


def main() -> int:
    if WORK.is_dir():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"工作目录：{WORK}")

    group_apply()
    group_qc()
    group_assemble()
    group_bgm()

    print("\n" + "=" * 52)
    print("全绿" if not fails else f"{len(fails)} 项失败")
    for f in fails:
        print(f"  - {f}")
    print(f"（测试工程保留在 {WORK}，可复查；不需要时直接删目录）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
