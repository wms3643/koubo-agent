"""koubo-agent-dsh 公共层 —— 路径、外部依赖定位、子进程与 ffprobe 封装。

设计边界（重要）：
    本层 **不做任何判断性决策**，只提供确定性能力：路径解析、命令拼装、进程执行、
    媒体探测、JSON 读写。所有"要不要删这一段""这次回退到哪"的判断，都由 agent 层
    （LLM 子代理）产出决策文件，再由本层机械执行并校验。

适配来源：skill `koubo-video-pipeline`（8 条红线）＋ 实战脚本
`Desktop/koubo-video-pipeline-scripts/`。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ============================================================================
#  本机已验证的外部依赖（skill `koubo-video-pipeline` 环境节）
# ============================================================================
PYTHON_EXE = Path(
    r"C:/Users/lenovo/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
)
WHISPER_MODEL_DIR = Path(r"D:/workbuddy工作目录/models/faster-whisper-large-v3")
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# 统一编码参数（skill 阶段2：照抄，禁止自调）
CRF_CUT = 18
PRESET_CUT = "medium"
AUDIO_BITRATE = "192k"

# 字幕样式（1920x1080 口播片，skill 阶段3：照抄，禁止自调）
SUBTITLE_FORCE_STYLE = (
    "FontName=Microsoft YaHei,FontSize=18,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BackColour=&H00000000,Outline=2,Shadow=1,Bold=-1,"
    "Alignment=2,MarginV=26"
)


def subtitle_force_style(margin_v: float | None = None) -> str:
    """字幕样式串。`margin_v` 非空时**只覆盖 MarginV 这一项**。

    ⚠️ 为什么要开这个口子：`docs/contracts.md` §三 把 `subtitle_occlusion`（字幕挡脸）
    的回退写成「改 `force_style`（先调 `MarginV`，**越大越往上**）后重烧」，
    但样式串此前是**常量**、没有任何覆盖入口 ⇒ 那条回退**根本没有执行手段**，
    属于"文档承诺了、代码做不到"。这里只补这一个可变项：其余仍照抄 skill，禁止自调
    （字号/描边/对齐改了就不是原设计意图了，遮挡问题只该由离底距离解决）。

    注意方向：`MarginV` 是**离底边的距离**，所以"字幕太高"要**减小**它。
    """
    if margin_v is None:
        return SUBTITLE_FORCE_STYLE
    return re.sub(r"MarginV=\d+", f"MarginV={int(round(margin_v))}", SUBTITLE_FORCE_STYLE)

# 语义阈值（与实战脚本一致，改动需同步 contracts.md）
GAP_MERGE_SECONDS = 5.0  # 保留段合并为同块的段间隔上限
REMAP_TOLERANCE = 0.6     # 字幕重映射容差
HALLUCINATION_GAP = 0.45  # 累积幻觉链判定：段间隔
HALLUCINATION_MAX_DUR = 0.5    # 幻觉嫌疑：段时长上限（物理上说不完）
HALLUCINATION_MIN_CHARS = 6    # 幻觉嫌疑：字数下限
MIN_SUBTITLE_SECONDS = 0.4
MAX_SUBTITLE_CHARS_WARN = 20  # 仅告警，不阻断（口播长句可接受）

# 六类必删的普查关键词（供 plan 阶段产出证据，不代替 LLM 判断）
CUT_KEYWORDS = [
    "删掉", "不要", "算了", "跳过", "试尝", "是不是", "没必要", "补录", "重来",
    "不讲", "讲错了", "这句", "废话", "重录", "不对", "没对", "往上走",
]


# ============================================================================
#  项目目录模型
# ============================================================================
@dataclass(frozen=True)
class Project:
    """一个口播剪辑项目的工作目录。

    目录约定（与 `WORKBUDDY_口播剪辑工具说明.md` 第2节一致）::

        <root>/
        ├─ input/          raw.mp4（原始录屏，唯一源素材，只读）
        ├─ edit/           transcript.json / cut_blocks.json / edit_decisions.json
        │                  subtitles/final.srt / plan_context.json / qc_report.json
        │                  tmpimg/（抓帧，勿放 /tmp）
        └─ renders/        edited_final.mp4 / subtitle_test_10s.mp4 / final.mp4
    """

    root: Path

    @classmethod
    def resolve(cls, raw: str | os.PathLike[str]) -> "Project":
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        return cls(root=p)

    # --- 子目录 ---
    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def edit_dir(self) -> Path:
        return self.root / "edit"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def tmpimg_dir(self) -> Path:
        return self.edit_dir / "tmpimg"

    @property
    def overlays_dir(self) -> Path:
        return self.root / "overlays"

    # --- 关键文件 ---
    @property
    def raw_video(self) -> Path:
        return self.input_dir / "raw.mp4"

    @property
    def transcript_json(self) -> Path:
        return self.edit_dir / "transcript.json"

    @property
    def transcript_txt(self) -> Path:
        return self.edit_dir / "transcript.txt"

    @property
    def transcript_srt(self) -> Path:
        return self.edit_dir / "transcript.srt"

    @property
    def edited_transcript(self) -> Path:
        return self.edit_dir / "edited_transcript.txt"

    @property
    def cut_blocks(self) -> Path:
        return self.edit_dir / "cut_blocks.json"

    @property
    def edit_decisions(self) -> Path:
        return self.edit_dir / "edit_decisions.json"

    @property
    def plan_context(self) -> Path:
        return self.edit_dir / "plan_context.json"

    @property
    def cut_report(self) -> Path:
        return self.edit_dir / "cut_report.json"

    @property
    def srt_report(self) -> Path:
        return self.edit_dir / "srt_report.json"

    @property
    def subtitle_fixes(self) -> Path:
        """字幕子代理的**独立**写回通道。

        刻意与 `edit_decisions.json` 分开：那个文件是逐字稿编辑子代理的产物，
        字幕子代理若覆盖它，会连带抹掉删除集。两个 agent 各自拥有一个文件。
        """
        return self.edit_dir / "subtitle_fixes.json"

    @property
    def timeline_map(self) -> Path:
        """剪辑**后**时间轴的权威索引（句级 + 词级），供动效层做音画同步。

        ⚠️ 动效层**绝不能**直接用 `edit/transcript.json` 的时间 —— 那是剪辑**前**的
        时间轴：每过一个剪点，后面的时间就累积偏移（实测素材删了 142.56s），
        拿它做的动画会越到后面错得越多。本文件由 stage3 在重映射时顺手产出，零额外成本。
        """
        return self.edit_dir / "timeline_map.json"

    @property
    def bgm_report(self) -> Path:
        return self.edit_dir / "bgm_report.json"

    @property
    def bgm_spec(self) -> Path:
        return self.edit_dir / "bgm.json"

    @property
    def bgm_dir(self) -> Path:
        return self.root / "bgm"

    @property
    def bgm_task(self) -> Path:
        return self.bgm_dir / "task.json"

    @property
    def qc_report(self) -> Path:
        return self.edit_dir / "qc_report.json"

    @property
    def final_srt(self) -> Path:
        return self.edit_dir / "subtitles" / "final.srt"

    @property
    def edited_video(self) -> Path:
        return self.renders_dir / "edited_final.mp4"

    @property
    def subtitle_test_video(self) -> Path:
        return self.renders_dir / "subtitle_test_10s.mp4"

    @property
    def final_video(self) -> Path:
        return self.renders_dir / "final.mp4"

    def ensure_dirs(self) -> None:
        for d in (self.input_dir, self.edit_dir, self.renders_dir, self.tmpimg_dir):
            d.mkdir(parents=True, exist_ok=True)


# ============================================================================
#  文件 / 子进程工具
# ============================================================================
def read_json(path: Path):
    """读 JSON，**容忍 UTF-8 BOM**。

    ⚠️ 为什么不是小事：`edit_decisions.json` / `subtitle_fixes.json` 是**外部 agent 写的**
    （`write` 工具或人手改），Windows 编辑器很容易带上 BOM。用 `encoding="utf-8"` 读会
    `json.decoder.JSONDecodeError: Expecting value: line 1 column 1` —— 报错文案完全指不到
    "其实是 BOM"，排查成本高。用 `utf-8-sig` 读则两种都能吃（无 BOM 时等价于 utf-8）。
    """
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload) -> None:
    """原子写（tmp + os.replace），避免中途中断留下半截 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)


def run(cmd: list[str], *, cwd: Path | None = None, capture: bool = True) -> str:
    """执行子进程。失败即抛错并带上 stderr 尾部，便于 agent 直接读到原因。"""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise RuntimeError(
            f"命令失败 (exit={proc.returncode}): {' '.join(cmd[:3])} ...\n{tail}"
        )
    return proc.stdout or ""


def run_merged(cmd: list[str], *, cwd: Path | None = None) -> str:
    """执行子进程并**把 stderr 合并进返回值**。

    ⚠️ 为什么必须有这个函数：ffmpeg 的滤波器统计（`volumedetect` 的 max_volume、
    `ebur128` 的 LUFS、`blackdetect` 的黑帧区间）**全部写在 stderr**，而 `run()` 只返回
    stdout ⇒ 用 `run()` 去解析这些统计会**永远匹配不到**，断言静默失效（比报错更危险：
    检查项"看起来通过"，其实从未运行）。
    所以凡是"跑 ffmpeg 滤波器并解析其统计"的地方，一律用本函数。
    """
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = proc.stdout or ""
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令失败 (exit={proc.returncode}): {' '.join(cmd[:3])} ...\n{text[-1500:]}"
        )
    return text


def run_bash_script(script: Path) -> str:
    """在 Git Bash 中执行生成的 .sh（Windows 下 ffmpeg filter 脚本走这条路径）。

    ⚠️ 必须传**文件名**而不是完整路径。真跑撞到过（2026-10-05）：
    Windows 绝对路径里的反斜杠会被 bash 当成转义符吃掉，`C:\\Users\\...\\run_cut.sh`
    到 bash 眼里变成 `C:Users...run_cut.sh` ⇒
    `/bin/bash: C:Userslenovo...run_cut.sh: No such file or directory`（exit 127）。
    后果是 **`cut-apply --run` 在任何 Windows 项目上都失败** —— 而它此前被
    "测试全绿"掩盖着：那些测试没有真正用 `--run` 走一遍编码。

    `cwd` 已经切到脚本所在目录，传 `script.name` 就够了，也就绕开了整个转义问题。
    """
    return run(["bash", script.name], cwd=script.parent)


def probe(video: Path) -> dict:
    """ffprobe 取规格：时长 / 分辨率 / 帧率 / 有无音视频流 / 编码。"""
    out = run(
        [
            FFPROBE, "-v", "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate",
            "-of", "json", str(video),
        ]
    )
    data = json.loads(out)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = 0.0
    if v and v.get("r_frame_rate") and "/" in v["r_frame_rate"]:
        num, den = v["r_frame_rate"].split("/")
        fps = round(float(num) / float(den), 3) if float(den) else 0.0
    return {
        "duration": round(float(data.get("format", {}).get("duration", 0.0)), 3),
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "fps": fps,
        "video_codec": v.get("codec_name") if v else None,
        "audio_codec": a.get("codec_name") if a else None,
        "has_video": v is not None,
        "has_audio": a is not None,
    }


def srt_timestamp(seconds: float) -> str:
    """秒 -> SRT 时间码 HH:MM:SS,mmm。"""
    negative = seconds < 0
    seconds = max(0.0, seconds)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:  # 浮点进位
        ms = 0
        seconds += 1
    total = int(seconds)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    code = f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return code


class KernelError(RuntimeError):
    """带 cause 的业务性失败：能被 agent 路由，而不是当成崩溃。"""

    def __init__(self, cause: str, message: str) -> None:
        super().__init__(message)
        self.cause = cause


def load_segments(transcript_json: Path) -> list[dict]:
    """读取转写文件并归一化为「段列表」。

    已见三种落盘格式，全部兼容（真值始终是 `list[{start,end,text,words?}]`）：
        1. 裸数组                       —— 本内核 stage1 与实战脚本的格式
        2. `{"segments":[...], "duration":n}`
        3. `{"language":..,"duration":n,"segments":[...]}`
    """
    data = read_json(transcript_json)
    if isinstance(data, dict) and "segments" in data:
        data = data["segments"]
    if not isinstance(data, list) or not data:
        raise KernelError(
            "transcript_bad_shape",
            f"{transcript_json} 不是非空段列表（实际 {type(data).__name__}）",
        )
    if not isinstance(data[0], dict) or "start" not in data[0]:
        raise KernelError(
            "transcript_missing_timestamps",
            f"{transcript_json} 的元素缺少 start/end 时间戳——"
            "请先跑 stage1 转写（纯文本逐字稿无法用于重映射字幕）",
        )
    return data


def is_suspect_hallucination(seg: dict) -> bool:
    """「幻觉嫌疑段」的唯一判定 —— plan 与 apply **共用**，避免两处规则漂移。

    判据：**时长 ≤ `HALLUCINATION_MAX_DUR`（0.5s）却 ≥ `HALLUCINATION_MIN_CHARS`（6）字** ——
    物理上说不完，几乎一定是 Whisper 的重复/累积幻觉。音频上那其实是一整句连贯的话，
    所以**绝不裁时间轴**（删了会把说话声切断），只在字幕层合并。
    """
    text = (seg.get("text") or "").strip()
    chars = len(text.replace(" ", "").replace("\u3000", ""))
    try:
        dur = float(seg["end"]) - float(seg["start"])
    except (KeyError, TypeError, ValueError):
        return False
    return dur <= HALLUCINATION_MAX_DUR and chars >= HALLUCINATION_MIN_CHARS


def remap_time(blocks: list[dict], t: float, *,
               tolerance: float = REMAP_TOLERANCE) -> float | None:
    """原片秒 → 剪辑后秒；落在任何保留块（含容差）之外返回 `None`。

    ⭐ **这是重映射的唯一实现**：`build_timeline()`（时间轴索引）与 `stage3_srt`
    都调它。此前两处各写了一份同样的循环 —— 规则一旦改（比如容差或"是否 clamp"），
    两份就会漂移，而漂移的结果是"字幕与时间轴对不上"这种最难查的错。
    """
    for b in blocks:
        if b["orig_start"] - tolerance <= t <= b["orig_end"] + tolerance:
            return b["edit_start"] + (t - b["orig_start"])
    return None


def build_timeline(
    segs: list[dict], blocks: list[dict], *, tolerance: float = REMAP_TOLERANCE
) -> tuple[list[dict], dict]:
    """把保留段的段级 / 词级时间，从**原片时间轴**重映射到**剪辑后时间轴**。

    这是「剪后时间轴」的**唯一实现** —— 由**剪裁策划**在剪切完成时立即调用并落盘
    （`edit/timeline_map.json`）。字幕与特效都消费这一份，不再各自重算。

    返回 `(segments, stats)`；每条记录同时含两套坐标：
        `source_start` / `source_end` —— 剪辑前（原片）秒，回答"这句原来在哪"
        `edit_start`   / `edit_end`   —— 剪辑后（成片）秒，回答"动效该在第几秒"
        `words[]` = `{w, s, e}`，其中 s/e 是**剪辑后**的词级时间（特效精确对齐要用）

    ⚠️ `stats` 是为了**不静默丢条**：契约写的是"时间轴条数 == 保留段数"，
    但空文本段 / 缺时间戳段 / 落在保留块之外的段会被跳过。以前这是静默的
    （只影响计数，看不出来），现在把三个跳过计数显式交给调用方落进报告。
    """
    keep_idx: list[int] = []
    for b in blocks:
        keep_idx.extend(range(b["first_seg"] - 1, b["last_seg"]))

    out: list[dict] = []
    skipped_empty = 0
    skipped_no_ts = 0
    skipped_unmapped = 0
    for i in keep_idx:
        if i >= len(segs):
            skipped_unmapped += 1
            continue
        s = segs[i]
        text = (s.get("text") or "").strip()
        if not text:
            skipped_empty += 1
            continue
        words = [w for w in (s.get("words") or []) if (w.get("word") or "").strip()]
        st = words[0]["start"] if words else s.get("start")
        en = words[-1]["end"] if words else s.get("end")
        if st is None or en is None:
            skipped_no_ts += 1
            continue
        ns, ne = remap_time(blocks, st, tolerance=tolerance), \
            remap_time(blocks, en, tolerance=tolerance)
        if ns is None or ne is None:
            skipped_unmapped += 1
            continue
        ws = []
        for w in words:
            a = remap_time(blocks, w["start"], tolerance=tolerance)
            b = remap_time(blocks, w["end"], tolerance=tolerance)
            if a is None or b is None:
                continue
            ws.append({"w": w["word"].strip(), "s": round(a, 3), "e": round(b, 3)})
        out.append({
            "source_start": round(st, 3),
            "source_end": round(en, 3),
            "edit_start": round(ns, 3),
            "edit_end": round(ne, 3),
            "text": text,
            "words": ws,
        })
    stats = {
        "keep_segments": len(keep_idx),
        "timeline_segments": len(out),
        "skipped_empty_text": skipped_empty,
        "skipped_no_timestamp": skipped_no_ts,
        "skipped_unmapped": skipped_unmapped,
    }
    return out, stats


# ---------------------------------------------------------------------------
#  元气中转站（api.lk888.ai）凭据解析
# ---------------------------------------------------------------------------
GATEWAY_BASE = os.environ.get("LK_BASE_URL", "https://api.lk888.ai/v1")
# 默认从桌面这份明文文档里读（保持密钥只有一处副本）。
# ⛔ 绝不把密钥写进任何配置文件/仓库；可用 LK_API_KEY / LK_API_KEY_FILE 覆盖。
DEFAULT_KEY_DOC = Path(r"C:/Users/lenovo/Desktop/元气中转站调用文档.md")


def resolve_api_key() -> tuple[str | None, str]:
    """返回 `(key, 来源说明)`。来源优先级：env LK_API_KEY → env YUANQI_API_KEY
    → env LK_API_KEY_FILE 指向的文件 → 默认桌面文档。找不到返回 `(None, 原因)`。"""
    for name in ("LK_API_KEY", "YUANQI_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v.startswith("sk-"):
            return v, f"env {name}"
    cand = os.environ.get("LK_API_KEY_FILE")
    path = Path(cand) if cand else DEFAULT_KEY_DOC
    if not path.is_file():
        return None, f"密钥文件不存在: {path}（可设 env LK_API_KEY 或 LK_API_KEY_FILE）"
    # ⚠️ 密钥字符集要放宽：网关的 key 可能含 `-` / `_`（此前只认字母数字，
    #    一旦网关换了 key 形态就会"读不到密钥"，而报错文案是"没找到 sk- 开头的密钥"，
    #    排查方向完全跑偏）。用「遇到空白/引号/反引号才停」的宽松匹配。
    m = re.search(r"sk-[A-Za-z0-9_\-]+", path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None, f"在 {path} 里没找到 sk- 开头的密钥"
    return m.group(0), f"file {path.name}"


def guarded(fn, *args, **kwargs):
    """stage 入口的统一守卫：把异常转成带 cause 的 JSON 报告 + 约定退出码。

    退出码：0 通过；2 业务性失败（可路由回退）；1 环境/代码异常（需人工看 traceback）。
    """
    import traceback

    try:
        return fn(*args, **kwargs)
    except KernelError as e:
        emit({"ok": False, "cause": e.cause, "error": str(e)}, exit_code=2)
    except FileNotFoundError as e:
        emit({"ok": False, "cause": "missing_file", "error": str(e)}, exit_code=2)
    except Exception as e:  # noqa: BLE001 —— 顶层兜底，必须留证
        emit(
            {
                "ok": False,
                "cause": "kernel_exception",
                "error": f"{type(e).__name__}: {e}",
                "traceback_tail": traceback.format_exc()[-1500:],
            },
            exit_code=1,
        )
    return None


def emit(payload: dict, *, exit_code: int = 0) -> None:
    """所有 stage 的统一出口：把结果打到 stdout 供 agent 读取。

    ⚠️ 中文在部分 Windows 终端可能不显示，agent 侧应以写入的 JSON 报告为准。
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    sys.stdout.flush()
    sys.exit(exit_code)
