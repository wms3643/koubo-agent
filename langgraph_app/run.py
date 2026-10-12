"""命令行入口 —— `python -m langgraph_app.run <项目目录> [选项]`

三条使用路径：

    1. 新跑一片           python -m langgraph_app.run projects/_smoke
    2. 有中断要拍板时     同上会停下并把问题打出来；看好后：
                          python -m langgraph_app.run projects/_smoke --thread <id> --approve
                          （或 --reject）
    3. 看当前状态         python -m langgraph_app.run projects/_smoke --status

设计取舍：

    · **一次调用只推进一步"人工决策"**。遇到 interrupt 就退出并打印问题，
      而不是在命令行里追问 —— 追问会让"AI 自己批准自己"变得唾手可得，
      而这条闸门存在的全部意义就是拦住那件事。
    · **默认不花钱**：`allow_paid` 默认 False，付费动作一定会先中断。
      跑自动化流程时显式传 `--allow-paid` 才跳过询问（那是知情选择）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from langgraph.types import Command

from . import stages as S
from .config import AppConfig
from .graph import build_graph, make_checkpointer, open_checkpoint_db
from .state import initial_state


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 钉成 UTF-8。**中文 Windows 上不这么做会直接崩。**

    真跑撞到过：控制台默认编码是 GBK，而本应用的输出里有 `⇒`（U+21D2）这类字符，
    `print()` 抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u21d2'`
    —— 报告都跑完写盘了，却在**最后打印那一下**崩掉，用户看到的是一条 traceback，
    很容易误以为"整个流水线崩了"。

    `errors="replace"` 是第二道保险：宁可显示成 `?`，也不要让运行崩在打印上。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 某些被重定向/替换过的流不支持
            pass


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def _default_thread_id(project: Path) -> str:
    """默认 thread = 项目目录名。同一项目天然续同一个 thread（断点续跑）。"""
    return project.name or "default"


def _show_interrupts(app, cfg) -> bool:
    """打印挂起的中断。返回 True 表示"确实在等人工回答"。"""
    snapshot = app.get_state(cfg)
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    if not interrupts:
        return False
    print("\n== 需要你拍板（流程已暂停，状态已保存）==")
    for it in interrupts:
        value = getattr(it, "value", it)
        if isinstance(value, dict) and value.get("type") == "approval_request":
            print(f"  项目节点：{value.get('from_node')}")
            print(f"  事由：{value.get('question')}")
            if value.get("price") is not None:
                print(f"  价格：{value['price']} 元/次")
            print(f"  批准键：{value.get('approval_key')}")
        else:
            _print_json(value)
    print("\n批准：加 --approve 再跑一次；拒绝：加 --reject。")
    print("（⛔ 不回答就等于不做 —— 这是有意设计：花钱与重跑长任务不该被自动批准）")
    return True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="langgraph_app.run",
        description="口播剪辑流水线（LangGraph 编排层）—— 驱动确定性内核 kb.py",
    )
    ap.add_argument("project", help="项目目录（须在 projects_root 之内）")
    ap.add_argument("--thread", default=None, help="断点续跑的 thread id（默认用项目名）")
    ap.add_argument("--status", action="store_true", help="只打印当前状态，不推进")
    ap.add_argument("--approve", action="store_true", help="批准挂起的拍板事项")
    ap.add_argument("--reject", action="store_true", help="拒绝挂起的拍板事项")
    ap.add_argument("--allow-paid", action="store_true",
                    help="预先授权付费动作（BGM 生成，约 0.49 元/次）；默认不授权")
    ap.add_argument("--allow-expensive", action="store_true",
                    help="预先授权「更贵的动作」（重转写 / 重渲全片等）")
    ap.add_argument("--no-overlay", action="store_true",
                    help="出无动效版（仅当你明确要这个版本；交付说明里会注明）")
    ap.add_argument("--bgm-source", choices=["generate", "user"], default="generate",
                    help="BGM 素材来源：generate=自己生成（默认），user=用 bgm/ 下用户给的曲目")
    ap.add_argument(
        "--override", default=None,
        choices=[S.OVERRIDE_RETRANSCRIBE, S.OVERRIDE_ALLOW_HALLUCINATION_CUT],
        help="⛔ 例外放行（主管独有，偏离红线）：retranscribe=强制重转写（剪辑决策/"
             "时间轴/字幕全部作废）；allow_hallucination_cut=放行删除幻觉嫌疑段")
    ap.add_argument("--override-reason", default="",
                    help="例外放行的理由（≥10 字）—— 例外必须是一次**有记录的决定**")
    ap.add_argument("--checkpoint-db", default=None, help="覆盖断点库位置")
    return ap


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)

    config = AppConfig.from_env()
    if args.checkpoint_db:
        config = replace(config, checkpoint_db=Path(args.checkpoint_db))

    # 例外放行的理由在这里就拦 —— 别等跑到一半（甚至花了钱）才发现理由不合规。
    # 内核侧也有一道（`override_reason_required`），这里是"更早、更清楚"。
    if args.override and len(args.override_reason.strip()) < 10:
        print(f"--override {args.override} 需要 --override-reason（≥10 字），"
              "因为例外必须是一次有记录的决定。", file=sys.stderr)
        return 1

    # 真跑要模型（判断节点全靠它）；只看状态就不用。早失败比跑一半失败好。
    needs_model = not args.status and not (args.approve or args.reject)
    problems = config.validate(require_model=needs_model)
    if problems:
        print("启动前自检不通过：", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        return 1

    try:
        project_dir = config.resolve_project_dir(args.project)
    except ValueError as e:
        print(f"项目路径不合法：{e}", file=sys.stderr)
        return 1

    thread_id = args.thread or _default_thread_id(project_dir)
    cfg = {"configurable": {"thread_id": thread_id}}

    conn = open_checkpoint_db(config)
    app = build_graph(config, checkpointer=make_checkpointer(conn))
    try:
        if args.status:
            snapshot = app.get_state(cfg)
            values = (snapshot.values if snapshot else {}) or {}
            if not values:
                # 还没有 LangGraph 断点 ⇒ 直接问**内核**（这就是"先问状态，再动"）。
                # 否则新项目上 `--status` 只会打出空对象，等于没用。
                from .kernel import KernelClient

                probe = KernelClient(config).status(project_dir)
                _print_json({
                    "thread_id": thread_id,
                    "langgraph_checkpoint": None,
                    "kernel_status": (probe.report if probe.ok
                                      else {"ok": False, "cause": probe.cause,
                                            "error": probe.report.get("error")}),
                })
                return 0 if probe.ok else 1
            _print_json({
                "thread_id": thread_id,
                "next": list(getattr(snapshot, "next", ()) or ()),
                "values": _brief(values),
            })
            _show_interrupts(app, cfg)
            return 0

        resume_snapshot = app.get_state(cfg)
        waiting = bool(getattr(resume_snapshot, "interrupts", ()) or ())

        if args.approve or args.reject:
            if not waiting:
                print("当前没有挂起的事项要拍板 —— 无需 --approve/--reject。", file=sys.stderr)
                return 1
            result = app.invoke(Command(resume=not args.reject), cfg)
        elif waiting:
            _show_interrupts(app, cfg)
            return 2
        else:
            result = app.invoke(initial_state(
                str(project_dir),
                goal={
                    "overlay_required": not args.no_overlay,
                    "bgm_source": args.bgm_source,
                    "allow_paid": bool(args.allow_paid),
                    "allow_expensive": bool(args.allow_expensive),
                    "override": ({"action": args.override, "reason": args.override_reason}
                                 if args.override else {}),
                },
            ), cfg)

        if _show_interrupts(app, cfg):
            return 2

        final = (result or {}).get("final") or {}
        halted = bool((result or {}).get("halts"))
        print("\n== 本次运行结束 ==")
        print(f"  thread_id : {thread_id}")
        print(f"  结果      : {'已停下（需人工介入）' if halted else '走到终点'}")
        if final:
            _print_json(final)
        return 2 if halted else 0
    finally:
        conn.close()


def _brief(values: dict) -> dict:
    """状态摘要（不要把整个 state 打出来 —— 里面有大段报告）。"""
    if not values:
        return {}
    route = values.get("route_history") or []
    return {
        "project": values.get("project"),
        "resume_from": values.get("resume_from"),
        "已完成的阶段": sorted((values.get("results") or {}).keys()),
        "回退次数": values.get("retakes") or {},
        "cause 流水": values.get("cause_history") or [],
        "最近裁决": route[-1] if route else None,
        "停下原因": [h.get("reason") for h in (values.get("halts") or [])],
        "notes": values.get("notes") or [],
    }


if __name__ == "__main__":
    raise SystemExit(main())
