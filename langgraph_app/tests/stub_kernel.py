"""桩内核 —— 按 `STUB_SCENARIO` 复现内核的各种"出口形态"。

为什么需要它：`KernelClient` 的价值全在**判读**上（退出码 → cause、乱码 stdout、
超时杀树、报告缺字段…）。这些分支如果只能靠真跑 ffmpeg/GPU 才能覆盖，
那它们**永远不会被测到** —— 而"断言看起来在跑、其实从未运行"正是本项目最忌讳的事。

它刻意**不 import** 内核任何代码：桩要伪装的是**协议**（stdout/退出码），
一旦复用了内核实现，协议变了桩也跟着变，就测不出漂移了。
"""

from __future__ import annotations

import json
import os
import sys
import time


def _emit(payload: dict, code: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    sys.stdout.flush()
    return code


def main() -> int:
    scenario = os.environ.get("STUB_SCENARIO", "ok")
    # `kb.py status` 之外所有子命令都带 --project，这里只做形态校验
    if "--project" not in sys.argv:
        sys.stderr.write("需要 --project\n")
        return 1

    if scenario == "ok":
        return _emit({"ok": True, "stage": "stub", "note": "正常通过"}, 0)

    if scenario == "business":
        # 业务性失败：退出码 2 + 可路由 cause
        return _emit({"ok": False, "stage": "stub", "cause": "index_out_of_range",
                      "error": "删除索引越界: [999] 不在 [0,572]（0-based）"}, 2)

    if scenario == "crash":
        # 环境/代码异常：退出码 1 + 栈尾（需人工看）
        return _emit({"ok": False, "cause": "kernel_exception",
                      "error": "ValueError: boom",
                      "traceback_tail": "Traceback (most recent call last): ..."}, 1)

    if scenario == "garbage":
        # 完全不可解析，但退出码是 2 ⇒ 只能按退出码定性
        sys.stdout.write("这不是 JSON，是内核把日志打进 stdout 了\n")
        return 2

    if scenario == "garbage_ok":
        sys.stdout.write("some noise, no json\n")
        return 0

    if scenario == "noisy":
        # 子进程噪声 + 合法报告 ⇒ 必须仍然解析出报告（_extract_json_object 的兜底）
        sys.stdout.write("ffmpeg version 7.0 ...\nframe= 100 fps=30\n")
        return _emit({"ok": True, "stage": "stub", "note": "噪声之后的报告"}, 0)

    if scenario == "multi_cause":
        # qc 形态：一次挂多项断言 ⇒ causes 是复数，编排层不能压成一个
        return _emit({"ok": False, "stage": "qc",
                      "checks": [{"name": "srt_bom", "ok": False, "cause": "srt_bom"},
                                 {"name": "audio_clipping", "ok": False, "cause": "audio_clipping"}],
                      "causes": ["srt_bom", "audio_clipping"]}, 2)

    if scenario == "no_ok_field":
        # 报告里没有 ok 字段 ⇒ 必须以退出码为准（0 就应当判为通过）
        return _emit({"stage": "stub", "note": "缺 ok 字段"}, 0)

    if scenario == "sleep":
        time.sleep(60)
        return _emit({"ok": True, "stage": "stub"}, 0)

    if scenario == "wrong_shape":
        # 顶层是数组（不是对象）⇒ 应被视为"无报告"
        sys.stdout.write(json.dumps([1, 2, 3]) + "\n")
        return 2

    sys.stderr.write(f"未知场景: {scenario}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
