"""内核调用层 —— 编排层与确定性内核之间**唯一**的边界。

它复刻的是 `docs/contracts.md` §一定义的工具契约，而不是自己发明一套：

    退出码：0 = 通过 ／ 2 = **业务性失败**（带 cause，可路由）／ 1 = 环境或代码异常（需人看栈）
    stdout：**只有一个 JSON 对象**（`pipeline/common.py` 的 `emit()` 保证）
    `guarded()` 的兜底映射：KernelError→2、FileNotFoundError→2（`missing_file`）、其它→1（`kernel_exception`）

三条设计取舍（都是有理由的，不是习惯）：

1. **一律走子进程，不 import 内核业务代码。**
   内核是"给定输入产出固定输出并自检"的确定性层，它的对外承诺就是 CLI + JSON 报告。
   进程边界让"内核崩了"与"编排错了"互不污染，也让内核可以独立测试。

2. **11 个工具 = 11 个显式方法，没有自由 `args` 数组。**
   这是本项目在 DSH 那版上踩过的坑：一个工具承载多个 stage、外加自由 `args` ⇒
   内核新增任何开关都**自动对调用方开放**，权限边界失效（contracts.md D-4.17）。
   所以这里把每个工具的参数**逐个写死成签名**，例外开关只出现在 `override_*` 方法里。

3. **永远返回同一个 `KernelResult`，绝不抛异常。**
   连"调用前就被拒"（如 `reason` 太短）也做成 `ok=False` 的结果对象，
   这样上层只有一条判读路径：`result.ok` / `result.causes`。少一条分支就少一处漂移。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig

# ---------------------------------------------------------------------------
#  退出码（与 pipeline/common.py 的 guarded() 约定一致，不要另立一套）
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_BUSINESS = 2
EXIT_ENV = 1

# 例外开关要求的最小理由长度（contracts.md：override_reason_required）
MIN_OVERRIDE_REASON_CHARS = 10


@dataclass(frozen=True)
class KernelResult:
    """一次内核调用的完整结果。**唯一的判读入口**。"""

    tool: str                      # 工具名（kb_transcribe / kb_cut / …）
    ok: bool
    cause: str | None = None
    exit_code: int | None = None
    report: dict = field(default_factory=dict)
    duration_s: float = 0.0
    cmd: tuple[str, ...] = ()
    stdout_tail: str = ""
    stderr_tail: str = ""
    timed_out: bool = False
    spawned: bool = True           # False = 调用前就被拒（参数不合法），没起进程

    # -- 供编排层判读的两个便利视图 -------------------------------------- #
    @property
    def causes(self) -> tuple[str, ...]:
        """本次调用产生的**全部** cause。

        ⚠️ 为什么是复数：`kb_qc` 一次可以挂多项断言，`qc_report.json` 里给的就是 `causes[]`。
        如果只暴露单个 cause，多因失败会被压成一个，回退矩阵就可能选错目标。
        """
        many = self.report.get("causes")
        if isinstance(many, list) and many:
            return tuple(str(c) for c in many)
        return (self.cause,) if self.cause else ()

    @property
    def summary(self) -> str:
        """给日志/报告用的一行摘要（不用于路由判断）。"""
        bits = [f"{self.tool}", "ok" if self.ok else f"FAIL cause={self.cause or '?'}"]
        if self.timed_out:
            bits.append(f"timeout>{self.duration_s:.0f}s")
        else:
            bits.append(f"{self.duration_s:.1f}s")
        return " | ".join(bits)

    def artifact(self, key: str, default=None):
        """从报告里取产物路径类字段（不同 stage 的键名不同，统一从报告读）。"""
        return self.report.get(key, default)


# ---------------------------------------------------------------------------
#  内部工具函数
# ---------------------------------------------------------------------------
def _extract_json_object(text: str) -> dict | None:
    """从 stdout 里取出那个 JSON 对象。

    为什么不能只 `json.loads(text)`：内核虽然承诺"只输出一个 JSON 对象"，但
    ffmpeg/hyperframes 等子进程**可能把输出混进同一个流**（`run()` 用 capture_output
    能隔开，`run_merged` 会合并 stderr）。所以按"第一个 `{` 到最后一个 `}`"再兜一次，
    比"解析失败就当作崩溃"更接近真相。
    """
    if not text or not text.strip():
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(stripped[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉子进程**及其后代**。

    ⚠️ 这不是洁癖：内核的调用链是 `kb.py → python stageX.py → ffmpeg`。
    只 kill 直接子进程的话，**ffmpeg 会变成孤儿继续占着 GPU/CPU 跑**，
    表现就是"超时之后机器更卡了"，而报告里写着"已终止"。
    Windows 用 `taskkill /T`（整棵树），POSIX 用进程组。
    """
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, check=False,
            )
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 —— 杀不掉也不能让收尾流程失败
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _child_env() -> dict[str, str]:
    """内核子进程的环境变量。

    ⚠️ 必须显式把 IO 编码钉成 UTF-8：内核的 JSON 报告里全是中文，
    在中文 Windows 上默认可能是 GBK ⇒ 我这边按 UTF-8 解码就会得到乱码，
    `json.loads` 失败 ⇒ 一次**成功的**调用被误判成 `kernel_crashed`。
    这类"编码导致假崩溃"是最难查的一类问题，所以在这里一次钉死。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# ---------------------------------------------------------------------------
#  内核客户端
# ---------------------------------------------------------------------------
class KernelClient:
    """把 `kb.py` 的 12 个子命令包成 11 个「一工具一方法」的调用面。

    工具 ↔ 方法 ↔ 内核子命令（与 contracts.md §一 的表格逐行对应）::

        kb_status    status()                     （只读）
        kb_plan_cut  plan_cut()                   cut-plan（只给证据）
        kb_transcribe transcribe()                transcribe
        kb_cut       cut()                        cut-apply [--run]
        kb_srt       srt()                        srt
        kb_burn      burn(mode)                   burn-test / burn-full
        kb_assemble  assemble(mode, spec, dry_run) assemble / mix
        kb_overlay   overlay(output)              overlay-render
        kb_qc        qc(target, frames_at)        qc
        kb_bgm       bgm(dry_run, confirm_paid)   bgm-generate
        kb_override  override_*()                 transcribe --force / cut-apply --allow-…
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    #  底层：执行一次内核调用
    # ------------------------------------------------------------------ #
    def _run(
        self,
        *,
        tool: str,
        argv: list[str],
        project: Path,
        timeout_s: float,
    ) -> KernelResult:
        cmd = [
            str(self.config.kernel_python),
            str(self.config.kb_entry),
            *argv,
            "--project",
            str(project),
        ]
        started = time.monotonic()
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            # 不弹出控制台窗口（长任务在后台跑时尤其明显）
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.config.kb_entry.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                **popen_kwargs,
            )
        except OSError as e:
            # 连进程都起不来（解释器丢了 / 路径错了）——这是环境问题，要人看
            return KernelResult(
                tool=tool, ok=False, cause="spawn_failed", exit_code=None,
                report={"ok": False, "cause": "spawn_failed", "error": str(e)},
                duration_s=round(time.monotonic() - started, 3),
                cmd=tuple(cmd), stderr_tail=str(e), spawned=False,
            )

        timed_out = False
        try:
            # ⚠️ 同时限时读 stdout/stderr：内核可能刷大量日志，
            #    单用 communicate(timeout=) 会在管道写满时假死，所以超时后立刻杀树再收尾。
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=20)
            except Exception:  # noqa: BLE001 —— 收尾拿不到输出也不能崩
                out, err = "", ""
        duration = round(time.monotonic() - started, 3)

        if timed_out:
            return KernelResult(
                tool=tool, ok=False, cause="timeout", exit_code=None,
                report={
                    "ok": False, "cause": "timeout", "timeout_s": timeout_s,
                    "error": f"内核执行超过 {timeout_s:.0f}s 被杀（已连同 ffmpeg 等后代进程一起终止）",
                },
                duration_s=duration, cmd=tuple(cmd),
                stdout_tail=(out or "")[-2000:], stderr_tail=(err or "")[-1000:],
                timed_out=True,
            )

        exit_code = proc.returncode
        report = _extract_json_object(out or "")
        if report is None:
            # 没有可解析的报告 ⇒ 只能按退出码定性（与插件 runKernel 的兜底一致）
            cause = (
                None if exit_code == EXIT_OK
                else "kernel_business_failure" if exit_code == EXIT_BUSINESS
                else "kernel_crashed"
            )
            return KernelResult(
                tool=tool, ok=exit_code == EXIT_OK, cause=cause, exit_code=exit_code,
                report={
                    "ok": exit_code == EXIT_OK, "cause": cause,
                    "error": "内核未返回可解析的 JSON 报告",
                },
                duration_s=duration, cmd=tuple(cmd),
                stdout_tail=(out or "")[-4000:], stderr_tail=(err or "")[-2000:],
            )

        # 报告能解析：**退出码与 `ok` 都必须为真**才算通过。
        #
        # ⚠️ 为什么两个都看：内核此前有 `ok:true` 却 `exit_code:2` 的地方
        #    （`stage4/cmd_full`、`stage5/cmd_assemble`、`cmd_mix` 的 checks 失败分支）。
        #    只信报告的调用方会把一次失败读成成功 —— 这就是"静默出片"。
        #    那种矛盾已在内核侧修掉，这里再兜一道、并且在报告里显式标出矛盾，
        #    这样即便将来又出现，也是**响亮地**出现，而不是悄悄放过。
        ok = exit_code == EXIT_OK and bool(report.get("ok", True))
        cause = report.get("cause")
        if report.get("ok") is True and exit_code != EXIT_OK and not cause:
            report = {**report, "exit_report_conflict": True,
                      "conflict_note": "报告写 ok=true 但退出码非 0 —— 按失败处理"}
            cause = "report_exit_conflict"
        if not ok and not cause:
            cause = ("kernel_business_failure" if exit_code == EXIT_BUSINESS
                     else "kernel_crashed" if exit_code else "unknown_failure")
        return KernelResult(
            tool=tool, ok=ok, cause=None if ok else cause, exit_code=exit_code,
            report=report, duration_s=duration, cmd=tuple(cmd),
            stdout_tail=(out or "")[-2000:], stderr_tail=(err or "")[-1000:],
        )

    # ------------------------------------------------------------------ #
    #  11 个工具（逐个写死，不给自由 args）
    # ------------------------------------------------------------------ #
    def status(self, project: Path) -> KernelResult:
        """`kb_status` —— 只读。断点续跑与回退前**必调**（不要凭记忆猜进度）。"""
        return self._run(
            tool="kb_status", argv=["status"], project=project,
            timeout_s=self.config.timeouts.status,
        )

    def plan_cut(self, project: Path) -> KernelResult:
        """`kb_plan_cut` —— 只产出证据（`plan_context.json`），不做判断。"""
        return self._run(
            tool="kb_plan_cut", argv=["cut-plan"], project=project,
            timeout_s=self.config.timeouts.fast,
        )

    def transcribe(
        self, project: Path, *, device: str | None = None, language: str | None = None
    ) -> KernelResult:
        """`kb_transcribe` —— GPU 长任务。参数刻意只留这两个（见 contracts.md §一）。"""
        argv = ["transcribe"]
        if device:
            if device not in ("cuda", "cpu"):
                return self._rejected("kb_transcribe", "bad_spec",
                                      f"device 只能是 cuda/cpu，实际 {device!r}")
            argv += ["--device", device]
        if language:
            argv += ["--language", language]
        return self._run(tool="kb_transcribe", argv=argv, project=project,
                         timeout_s=self.config.timeouts.transcribe)

    def cut(self, project: Path, *, run: bool = False) -> KernelResult:
        """`kb_cut` —— 按 `edit_decisions.json` 落地并断言越界/幻觉段。

        ⚠️ 删幻觉嫌疑段会被内核以 `hallucination_cut_forbidden` 拒掉（**绕不过去**），
        那是设计意图：例外只能由 `override_allow_hallucination_cut()` 放行。
        """
        argv = ["cut-apply"] + (["--run"] if run else [])
        return self._run(tool="kb_cut", argv=argv, project=project,
                         timeout_s=self.config.timeouts.encode)

    def srt(self, project: Path) -> KernelResult:
        """`kb_srt` —— 按保留块重映射生成 `final.srt`（确定性，无 LLM 参与）。"""
        return self._run(tool="kb_srt", argv=["srt"], project=project,
                         timeout_s=self.config.timeouts.fast)

    def burn(
        self, project: Path, *, mode: str, margin_v: float | None = None
    ) -> KernelResult:
        """`kb_burn` —— `mode="test"` 只烧前 10s 并抓帧；`mode="full"` 烧全片。

        ⚠️ `full` 有护栏：没有测试片 + 抓帧证据会被内核/上层拒绝（skill 红线第 7 条）。

        `margin_v` 是 `subtitle_occlusion`（字幕挡脸）回退的**唯一**可调项
        （见 `pipeline/common.subtitle_force_style()`）：它是**离底边距离，越大越往上**。
        """
        if mode not in ("test", "full"):
            return self._rejected("kb_burn", "bad_spec", f"mode 只能是 test/full，实际 {mode!r}")
        argv = [f"burn-{mode}"]
        if margin_v is not None:
            argv += ["--margin-v", f"{margin_v:g}"]
        return self._run(
            tool="kb_burn", argv=argv, project=project,
            timeout_s=self.config.timeouts.encode if mode == "full" else self.config.timeouts.fast,
        )

    def assemble(
        self, project: Path, *, mode: str, spec: str, dry_run: bool = False
    ) -> KernelResult:
        """`kb_assemble` —— `mode="assemble"` 做 xfade 缝接（可叠加动效层）；`mode="mix"` 铺 BGM。

        `dry_run=True` 时**真的不渲染**：只校验前置 + 按时长递推预测（见 contracts.md §一）。
        """
        if mode not in ("assemble", "mix"):
            return self._rejected("kb_assemble", "bad_spec", f"mode 只能是 assemble/mix，实际 {mode!r}")
        argv = [mode, "--spec", spec] + (["--dry-run"] if dry_run else [])
        return self._run(tool="kb_assemble", argv=argv, project=project,
                         timeout_s=self.config.timeouts.encode)

    def overlay(self, project: Path, *, output: str | None = None) -> KernelResult:
        """`kb_overlay` —— 渲染 `overlays/` 下的透明层工程（ProRes4444 带 alpha）。"""
        argv = ["overlay-render"] + (["--output", output] if output else [])
        return self._run(tool="kb_overlay", argv=argv, project=project,
                         timeout_s=self.config.timeouts.encode)

    def qc(
        self, project: Path, *, target: str, frames_at: list[float] | None = None
    ) -> KernelResult:
        """`kb_qc` —— 只断言 + 取证。

        ⚠️ `target` 决定**检查项集合**（contracts.md §一「检查项的作用域」）：
        拿全片 SRT 去比 10s 测试片会恒报假 `srt_timeline_mismatch`。
        所以这里 `target` 是必填、且必须是枚举内的值。
        """
        allowed = ("subtitle_test", "edited", "subtitled", "final")
        if target not in allowed:
            return self._rejected("kb_qc", "bad_spec",
                                  f"target 必须是 {allowed} 之一，实际 {target!r}")
        argv = ["qc", "--target", target]
        if frames_at:
            argv += ["--frames-at", ",".join(f"{t:g}" for t in frames_at)]
        return self._run(tool="kb_qc", argv=argv, project=project,
                         timeout_s=self.config.timeouts.fast)

    def bgm(
        self, project: Path, *, dry_run: bool = False, confirm_paid: bool = False,
        spec: str = "edit/bgm.json",
    ) -> KernelResult:
        """`kb_bgm` —— **唯一会花钱**的工具（suno-v4.5，约 0.49 元/次）。

        两道闸门都在内核里（本层只负责传对开关）：
          · 不传 `confirm_paid` ⇒ 只返回价格与提示（`bgm_needs_confirm`），**绝不提交**；
          · 已有成品 ⇒ 直接 `bgm_exists`（免费），不会重复扣费。
        ⛔ `--force` **刻意不暴露**：重复生成必须经过"用户拍板"这条人工路径，
        而不是一个可以让编排层随手带上的开关（contracts.md D-4.15）。
        """
        argv = ["bgm-generate", "--spec", spec]
        if dry_run:
            argv += ["--dry-run"]
        if confirm_paid:
            argv += ["--confirm-paid", "--max-price", str(self.config.budgets.bgm_max_price)]
        return self._run(tool="kb_bgm", argv=argv, project=project,
                         timeout_s=self.config.timeouts.bgm)

    # ------------------------------------------------------------------ #
    #  kb_override —— 例外开关：只有"主管"能调，且必须写明理由
    # ------------------------------------------------------------------ #
    def override_retranscribe(self, project: Path, *, reason: str) -> KernelResult:
        """⛔ 强制重转写（覆盖已有逐字稿）。

        代价：剪辑决策 / 剪后时间轴 / 字幕**全部作废**，整条下游重跑。
        调用前应先取得用户确认 —— 所以这个方法的调用点必须在人工中断**之后**。
        """
        bad = self._check_reason("kb_override(retranscribe)", reason)
        if bad:
            return bad
        return self._run(tool="kb_override(retranscribe)",
                         argv=["transcribe", "--force"], project=project,
                         timeout_s=self.config.timeouts.transcribe)

    def override_allow_hallucination_cut(self, project: Path, *, reason: str) -> KernelResult:
        """⛔ 放行"删幻觉嫌疑段"并执行裁切。

        代价：可能把一整句连贯说话切断（历史效果差的根因）。
        只有复核证据后确认"那段确实该从时间轴消失"才用。
        """
        bad = self._check_reason("kb_override(allow_hallucination_cut)", reason)
        if bad:
            return bad
        return self._run(tool="kb_override(allow_hallucination_cut)",
                         argv=["cut-apply", "--allow-hallucination-cut", "--run"],
                         project=project, timeout_s=self.config.timeouts.encode)

    # ------------------------------------------------------------------ #
    def _check_reason(self, tool: str, reason: str) -> KernelResult | None:
        """例外开关的理由校验：**例外必须是一次有记录的决定**，不是形式主义。"""
        text = (reason or "").strip()
        if len(text) < MIN_OVERRIDE_REASON_CHARS:
            return KernelResult(
                tool=tool, ok=False, cause="override_reason_required", exit_code=None,
                report={
                    "ok": False, "cause": "override_reason_required",
                    "error": f"reason 至少 {MIN_OVERRIDE_REASON_CHARS} 字，实际 {len(text)} 字",
                },
                spawned=False,
            )
        return None

    def _rejected(self, tool: str, cause: str, message: str) -> KernelResult:
        """调用前就被拒（参数不合法）—— 合成一个结果对象，**不抛异常**。"""
        return KernelResult(
            tool=tool, ok=False, cause=cause, exit_code=None,
            report={"ok": False, "cause": cause, "error": message},
            spawned=False,
        )
