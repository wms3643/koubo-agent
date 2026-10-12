"""BGM 生成（suno-v4.5）的端到端测试 —— 用**本地桩 HTTP 服务**，不花真钱。

为什么必须用桩而不是真网关：
    真跑一次 suno 会**扣费**（约 0.49 元/次），且不可重复测。用桩可以把
    "正常 / 余额不足 / 生成失败 / 超预算 / 无密钥" 五条路径都覆盖到，还能断言
    **关键的防重复扣费行为**（已有未完成任务时必须续轮询、不得再次 POST）。

被测对象是**真 CLI**（`kb.py bgm-generate`，子进程 + env 指向桩服务），
所以覆盖的是真实调用链，不是被 mock 掉的一层皮。

用例：
    A 干跑         → 只查价、不提交
    B 正常生成     → 提交 → 轮询 → 下载 → 报告 ok
    C 余额不足     → bgm_insufficient_balance
    D 生成失败     → bgm_generate_failed
    E 续轮询       → ⭐ 不重复 POST（防重复扣费）
    F 超预算       → bgm_price_above_budget
    G 无密钥       → bgm_no_api_key
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.common import PYTHON_EXE, Project, read_json  # noqa: E402

PROJ = ROOT / "projects" / "_selftest_bgmgen"
PRICE = 0.4896
FAKE_KEY = "sk-selftest-not-a-real-key"

# 桩的全局状态（每个用例前重置）
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
        path = self.path.split("?")[0]
        if path.endswith("/pricing"):
            if STATE.get("no_price"):
                return self._json(500, {"error": "stub: pricing unavailable"})
            price = STATE.get("price", PRICE)
            return self._json(200, {"channel_groups": [
                {"group_name": "MM", "is_active": True, "in_key_whitelist": True,
                 "base_price": price, "min_price": price}]})
        if path.endswith("/task-status"):
            STATE["polls"] = STATE.get("polls", 0) + 1
            if STATE.get("poll_402"):
                return self._json(402, {"error": "Insufficient balance"})
            if STATE["polls"] < STATE.get("final_after", 1):
                return self._json(200, {"code": 200, "data": {
                    "task_id": int(STATE["task_id"]) if STATE["task_id"].isdigit() else STATE["task_id"],
                    "state": "running", "is_final": False, "progress": 0.3}})
            if STATE.get("outcome") == "failed":
                return self._json(200, {"code": 200, "data": {
                    "task_id": STATE["task_id"], "state": "failed",
                    "is_final": True, "error": "stub: generation failed"}})
            return self._json(200, {"code": 200, "data": {
                "task_id": STATE["task_id"], "state": "success", "is_final": True,
                "progress": 1.0, "cost": PRICE, "refunded": False,
                "result_url": f"{STATE['base']}/dl/bgm.mp3"}})
        if path.endswith("/dl/bgm.mp3"):
            payload = b"\xff\xfb" + b"\x00" * 40_000  # >10KB，够过"文件太小"检查
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._json(404, {"error": f"stub: no route {path}"})

    def do_POST(self):
        if self.path.endswith("/media/generate"):
            STATE["posts"] = STATE.get("posts", 0) + 1
            if STATE.get("submit_402"):
                return self._json(402, {"error": "Insufficient balance"})
            STATE["last_body_bytes"] = len(self.rfile.read(
                int(self.headers.get("Content-Length") or 0)))
            # ⭐ 忠实复现真网关形态：载荷包在 `data` 里，且 task_id 是**整数**。
            #    真跑时就是因为"只认顶层字符串"而误判成提交失败（钱已扣却没记下 id）。
            tid = STATE["task_id"]
            return self._json(200, {"code": 200, "msg": "任务创建成功",
                                    "data": {"task_id": int(tid) if tid.isdigit() else tid,
                                             "task_ids": [tid],
                                             "成功数量": 1}})
        self._json(404, {"error": f"stub: no route {self.path}"})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_kb(args: list[str], env: dict, timeout: int = 120) -> tuple[int, dict]:
    p = subprocess.run([str(PYTHON_EXE), str(ROOT / "kb.py"), *args],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=timeout)
    out = (p.stdout or "").strip()
    try:
        obj = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        obj = {"_raw_stdout": out[-600:], "_stderr": (p.stderr or "")[-600:]}
    return p.returncode, obj


def main() -> int:
    if PROJ.is_dir():
        shutil.rmtree(PROJ)
    project = Project.resolve(PROJ)
    project.ensure_dirs()

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    STATE["base"] = base
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"桩服务: {base}")

    env = {**os.environ, "LK_BASE_URL": base + "/v1", "LK_API_KEY": FAKE_KEY}
    env.pop("LK_API_KEY_FILE", None)
    fails = 0

    def expect(cond: bool, msg: str) -> None:
        nonlocal fails
        print(f"    {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            fails += 1

    def reset(**kw) -> None:
        STATE.clear()
        STATE.update({"base": base, "task_id": "160174096", "outcome": "success",
                      "final_after": 1, "posts": 0, "polls": 0})
        STATE.update(kw)
        for p in (project.bgm_task, project.bgm_report):
            p.unlink(missing_ok=True)
        for p in project.bgm_dir.glob("bgm.*"):
            p.unlink()

    spec = project.edit_dir / "bgm.json"
    spec.write_text(json.dumps({"style": "Stub test style", "output": "bgm/bgm.mp3"},
                               ensure_ascii=False), encoding="utf-8")

    print("\n=== A 干跑（--dry-run，不该提交）===")
    reset()
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--dry-run"], env)
    expect(rc == 0 and obj.get("dry_run") is True, f"exit=0 且 dry_run（rc={rc}）")
    expect(obj.get("price_per_call") == PRICE, f"读到价格 {obj.get('price_per_call')}")
    expect(STATE["posts"] == 0, f"未提交（桩收到 POST {STATE['posts']} 次）")

    print("\n=== A2 付费闸门（内核级）：不带 --confirm-paid 绝不提交 ===")
    reset()
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ)], env)
    expect(rc == 2 and obj.get("cause") == "bgm_needs_confirm",
           f"cause=bgm_needs_confirm（实得 {obj.get('cause')}）")
    expect(STATE["posts"] == 0, f"**一次都没提交**（POST {STATE['posts']} 次）⭐ 防花钱")
    expect(obj.get("price_per_call") == PRICE,
           f"仍回报价格 {obj.get('price_per_call')}，便于人决定是否确认")

    print("\n=== B 正常生成 ===")
    reset(final_after=2)
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--confirm-paid"], env)
    out_file = PROJ / "bgm" / "bgm.mp3"
    expect(rc == 0 and obj.get("ok") is True, f"exit=0 且 ok（rc={rc}, {obj.get('cause')}）")
    expect(out_file.is_file() and out_file.stat().st_size > 10_000,
           f"音频落地 {out_file.stat().st_size if out_file.is_file() else 0} 字节")
    expect(STATE["polls"] >= 2, f"轮询了 {STATE['polls']} 次（首轮未 final）")
    expect(project.bgm_report.is_file(), "产出 edit/bgm_report.json")
    # ⭐ 回归锁：网关把 task_id 包在 `data` 里且是**整数** —— 必须能解析出来。
    #    真跑时因"只认顶层字符串"误判成 bgm_submit_failed（任务已创建、钱已扣）。
    expect(obj.get("task_id") == "160174096",
           f"从 data 里解析出整数 task_id（实得 {obj.get('task_id')!r}）")

    print("\n=== C 余额不足 ===")
    reset(submit_402=True)
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--confirm-paid"], env)
    expect(rc == 2 and obj.get("cause") == "bgm_insufficient_balance",
           f"cause={obj.get('cause')}")

    print("\n=== D 生成失败 ===")
    reset(outcome="failed")
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--confirm-paid"], env)
    expect(rc == 2 and obj.get("cause") == "bgm_generate_failed", f"cause={obj.get('cause')}")

    print("\n=== E 续轮询：不得重复提交（防重复扣费）===")
    reset(final_after=99)  # 第一次跑到超时，留下未完成任务
    _run_kb(["bgm-generate", "--project", str(PROJ), "--timeout", "1", "--confirm-paid"], env)
    posts_after_first = STATE["posts"]
    saved = read_json(project.bgm_task)
    expect(posts_after_first == 1 and saved.get("state") == "polling",
           f"首跑提交 1 次并留下 polling 状态（{saved.get('state')}）")
    reset_keep_task = json.loads(project.bgm_task.read_text(encoding="utf-8"))
    STATE.update({"base": base, "task_id": "160174096", "outcome": "success",
                  "final_after": 1, "polls": 0, "posts": 0})
    project.bgm_task.write_text(json.dumps(reset_keep_task, ensure_ascii=False),
                                encoding="utf-8")
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--confirm-paid"], env)
    expect(STATE["posts"] == 0, f"续跑**没有再 POST**（POST {STATE['posts']} 次）⭐")
    expect(obj.get("resumed_task") == "160174096" and obj.get("ok") is True,
           f"续轮询到成功（resumed={obj.get('resumed_task')}）")

    print("\n=== E2 已有产物 → 幂等闸（不得再花钱）===")
    reset()
    _run_kb(["bgm-generate", "--project", str(PROJ), "--confirm-paid"], env)   # 先造出 bgm.mp3
    posts_before = STATE["posts"]
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--confirm-paid"], env)
    expect(rc == 2 and obj.get("cause") == "bgm_exists",
           f"第二次调用被幂等闸拦住（cause={obj.get('cause')}）")
    expect(STATE["posts"] == posts_before,
           f"**没有再提交**（POST {STATE['posts']} 次，之前 {posts_before} 次）⭐ 防重复扣费")
    expect(bool(obj.get("existing_style")), "回报了已有产物的风格，便于判断是否要换")

    print("\n=== F 价格超预算 ===")
    reset(price=50.0)
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--max-price", "1", "--confirm-paid"], env)
    expect(rc == 2 and obj.get("cause") == "bgm_price_above_budget", f"cause={obj.get('cause')}")
    expect(STATE["posts"] == 0, "超预算时未提交")

    print("\n=== G 无密钥 ===")
    reset()
    env_nokey = {k: v for k, v in env.items() if k != "LK_API_KEY"}
    env_nokey["LK_API_KEY_FILE"] = "D:/definitely/not/here.md"
    env_nokey.pop("YUANQI_API_KEY", None)
    rc, obj = _run_kb(["bgm-generate", "--project", str(PROJ), "--dry-run"], env_nokey)
    expect(rc == 2 and obj.get("cause") == "bgm_no_api_key", f"cause={obj.get('cause')}")

    srv.shutdown()
    print("\n" + "=" * 46)
    print(f"结果：{'全绿' if fails == 0 else str(fails) + ' 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
