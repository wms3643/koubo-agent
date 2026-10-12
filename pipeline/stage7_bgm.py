r"""阶段 7 — BGM 素材：生成（suno-v4.5）→ 落地

为什么需要这一步：
    `mix` 的 `bgm` 只是**路径**。素材从哪来，原先是个悬空环节。
    元气中转站有 `suno-v4.5`（纯器乐模式），所以**默认由剪裁策划自己生成**；
    用户指定了现成曲目时才用用户的文件。见 docs/contracts.md「BGM」一节。

调用链（元气中转站，全部走 `Authorization: Bearer`）：
    1) GET  /skills/models/suno-v4.5/pricing    —— 付费前**必查**价格，超预算不提交
    2) POST /media/generate                     —— 拿 task_id，**立刻落盘**
    3) GET  /skills/task-status?task_id=...     —— 轮询到 is_final
    4) 下载 result_url                           —— 存成 bgm/bgm.<ext>

⛔ 两条防呆（都来自官方文档的告警）：
    - **不要因为进度暂时为零就重复提交** ⇒ 每次提交前后都写 `bgm/task.json`；
      若已有未完成任务，**续轮询**而不是重新提交（否则重复扣费）。
    - **402 / 余额不足时不要反复提交** ⇒ 直接返回 `bgm_insufficient_balance` 停下。

⚠️ Suno 单曲**最长 4 分钟**。口播片常 20 分钟 ⇒ `mix` 默认开 `bgm_loop`
   （`-stream_loop -1` 循环输入再 `atrim` 到片长），否则音乐会在中途断掉。

cause：
    bad_spec / bgm_no_api_key / bgm_price_above_budget / bgm_insufficient_balance
    bgm_submit_failed / bgm_generate_failed / bgm_timeout / bgm_download_failed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import (  # noqa: E402
    GATEWAY_BASE, Project, emit, guarded, read_json, resolve_api_key, write_json,
)

MODEL = "suno-v4.5"

# 口播片默认风格：无人声、克制、留足动态余量给人声，且便于循环。
DEFAULT_STYLE = (
    "Instrumental only, no vocals, no choir. Calm restrained underscore for a Chinese "
    "talking-head lecture video. Soft piano and warm sustained pad, gentle steady pulse, "
    "sparse arrangement, wide dynamic headroom, no drum fills, no sudden swells, "
    "loop-friendly, sits behind narration without masking the voice."
)


def _http_json(method: str, url: str, key: str, payload: dict | None = None,
               timeout: int = 60) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": body[:600]}
    except Exception as e:  # 网络层
        return 0, {"error": f"{type(e).__name__}: {e}"}


_URL_KEYS = ("result_url", "audio_url", "url", "download_url", "file_url", "result_urls",
             "result_url_list", "audios", "files")


def _find_url(obj) -> str | None:
    """挖出结果下载地址。兼容多种字段名与**列表**（Suno 常一次返回 2 首）。"""
    if isinstance(obj, dict):
        for k in _URL_KEYS:
            v = obj.get(k)
            got = _find_url(v)
            if got:
                return got
        for v in obj.values():
            got = _find_url(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_url(v)
            if got:
                return got
    elif isinstance(obj, str) and obj.startswith("http"):
        return obj
    return None


def _download(url: str, dest: Path, key: str) -> str | None:
    """下载产物。失败返回错误字符串，成功返回 None。"""
    last = "下载失败"
    for with_auth in (True, False):
        req = urllib.request.Request(url)
        if with_auth:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.read())
            if dest.stat().st_size > 10_000:
                return None
            last = f"文件太小（{dest.stat().st_size} 字节），疑非音频"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            continue
    return last


def _unwrap(resp) -> dict:
    """网关把真正的载荷放在 `data` 里（外层还有 `code`/`msg`）。

    实测响应：`{"code":200,"data":{"task_id":160174096,...},"msg":"任务创建成功"}`。
    ⛔ 不要把外层直接当数据用 —— 否则取不到 `task_id`（真跑时踩过：
    任务已创建、钱已扣，本地却判成"提交失败"，还差点重复提交）。
    """
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return resp if isinstance(resp, dict) else {}


def _find_task_id(obj) -> str | None:
    """在响应里挖出 task_id。⚠️ 实测是**整数**（`160174096`），必须兼容 int。"""
    if isinstance(obj, dict):
        for k in ("task_id", "taskId"):
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v)
        for v in obj.values():
            got = _find_task_id(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_task_id(v)
            if got:
                return got
    return None


def cmd_generate(project: Project, spec_path: Path, *, dry_run: bool, max_price: float,
                 timeout: int, force: bool = False, confirm_paid: bool = False) -> int:
    project.bgm_dir.mkdir(parents=True, exist_ok=True)

    spec = {}
    if spec_path.is_file():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    style = (spec.get("style") or DEFAULT_STYLE).strip()
    out_rel = spec.get("output") or "bgm/bgm.mp3"
    output = project.root / out_rel
    budget = float(spec.get("max_price", max_price))
    poll_timeout = int(spec.get("poll_timeout", timeout))

    key, src = resolve_api_key()
    if not key:
        emit({"ok": False, "stage": "bgm-generate", "cause": "bgm_no_api_key",
              "error": src, "next": "report_to_user"}, exit_code=2)
        return 2

    # ── 1) 付费前必查价格 ──
    # ⚠️ 必须 `_unwrap`：网关把真正的载荷包在 `data` 里（本文件 `_unwrap` 的注释记着这个
    #    实测事实）。此前 pricing 直接读顶层 `channel_groups`，一旦响应被包进 `data`，
    #    `price` 会静默变成 None —— 预算闸门**看起来还在、其实已经不检查了**。
    status, pricing_raw = _http_json(
        "GET", f"{GATEWAY_BASE}/skills/models/{MODEL}/pricing", key)
    pricing = _unwrap(pricing_raw)
    price = None
    if status == 200:
        groups = [g for g in (pricing.get("channel_groups") or [])
                  if isinstance(g, dict) and g.get("is_active") and g.get("in_key_whitelist")]
        if groups:
            price = min(float(g.get("min_price") or g.get("base_price") or 0) for g in groups)
    if price is not None and price > budget:
        emit({"ok": False, "stage": "bgm-generate", "cause": "bgm_price_above_budget",
              "error": f"预计 {price} 元/次 > 预算 {budget} 元",
              "price": price, "budget": budget}, exit_code=2)
        return 2

    plan = {
        "ok": True, "stage": "bgm-generate", "model": MODEL, "key_source": src,
        "price_per_call": price, "price_available": price is not None,
        "budget": budget, "output": str(output),
        "style": style, "duration_limit_note": "Suno 单曲最长 4 分钟；长片靠 mix 的 bgm_loop 循环",
        "pricing_http": status,
    }
    if dry_run:
        plan["dry_run"] = True
        plan["next"] = ("确认价格后去掉 --dry-run 真跑" if price is not None
                        else "⚠️ 未能查到价格（pricing HTTP "
                             f"{status}）—— 真跑前请先修通查价，内核不允许在"
                             "「不知道单价」的情况下提交付费任务")
        emit(plan, exit_code=0)
        return 0

    # ── 2) 续轮询优先（**不花钱**）：已有未完成任务就直接接着轮询 ──
    #    注意顺序：**续轮询要在"确认付费"之前** —— 那笔钱已经扣过了，
    #    卡在这里会让已付费的任务永远拿不回来。
    task_id = None
    resumed = False
    if project.bgm_task.is_file():
        prev = json.loads(project.bgm_task.read_text(encoding="utf-8"))
        if prev.get("state") not in ("success", "failed"):
            task_id = prev.get("task_id")
            if task_id:
                resumed = True
                plan["resumed_task"] = task_id

    # ── 3) 免费闸门：已有可用成品 ⇒ 直接复用，不谈钱 ──
    #    ⚠️ 顺序很重要：以前"已有成品"排在"付费确认"**后面**，于是重复调用会先拿到
    #    `bgm_needs_confirm`。那个提示会引导 agent 去加 `--confirm-paid` —— 方向是错的
    #    （其实什么都不用做）。免费的结论必须排在付费确认之前。
    if output.is_file() and output.stat().st_size > 10_000 and not force:
        prev = read_json(project.bgm_report) if project.bgm_report.is_file() else {}
        emit({**plan, "ok": False, "cause": "bgm_exists",
              "error": f"{output} 已存在（{output.stat().st_size} 字节），未重复生成",
              "existing_style": prev.get("style"),
              "existing_cost": prev.get("cost"),
              "hint": "已有可用 BGM，直接进 mix（kb_run stage=\"mix\"）即可。"
                      "确实要换一首：把 bgm/ 下的成品与 edit/bgm_report.json 移走后重跑 —— "
                      "`--force` **没有**暴露给模型（避免随手重复付费），只能由部署侧使用。"
                      "换一首会**再扣一次费**（约 0.49 元）。",
              }, exit_code=2)
        return 2

    # ── 4) ⛔ 付费闸门（内核级，不依赖任何插件机制）──
    #    为什么放在内核而不是插件护栏：
    #    `ctx.tools.guard()` 的作用域语义依赖调用它的 ctx 落哪一层
    #    （`guardReason` 只查 `layers.global` 与 `exec.agent` 的 scope 链），
    #    2026-10-05 实测出现过"桩里拦得住、真跑没拦、真花了 0.4896 元"。
    #    ⇒ **花钱这件事必须闸在能被测试证明的地方**：内核里的一道显式确认。
    #    想省钱先 `--dry-run` 查价；确认要花就显式带 `--confirm-paid`。
    #    ⚠️ 顺序：它必须排在"查价是否可用"之前 —— 契约写明"不传 `confirm_paid`
    #       只返回价格与提示、**绝不提交**"，这是模型最常撞到的第一道门。
    if not resumed and not confirm_paid:
        emit({**plan, "ok": False, "cause": "bgm_needs_confirm",
              "error": "这是**付费**操作（suno-v4.5 按次计费），尚未确认，因此没有提交",
              "price_per_call": price, "budget": budget,
              "hint": "① 先 `args:[\"--dry-run\"]` 只查价不花钱；"
                      "② 确认要花就显式 `args:[\"--confirm-paid\"]`（约 0.49 元/次）。"
                      "同一任务重复调用会**续轮询**，不会重复扣费。",
              }, exit_code=2)
        return 2

    # ── 5) ⛔ 查不到价 ⇒ 不许提交（"付费前必查价"不是"查得到才查"）──
    #    此前 price=None（pricing 报错/超时/结构变了）会被**静默放过**，直接进入提交 ——
    #    等于最该拦的那次（信息不全）恰恰没拦。宁可停下，也不在不知道单价时花钱。
    if not resumed and price is None:
        emit({**plan, "ok": False, "cause": "bgm_pricing_unavailable",
              "error": f"未能查到 {MODEL} 的单价（pricing HTTP {status}），"
                       "为避免在「不知道单价」的情况下付费，已拒绝提交",
              "response": pricing_raw,
              "hint": "① 先确认网关可达、密钥有该模型权限；② 用 args:[\"--dry-run\"] 复查看价"
                      "结果；③ 若已确认单价且可接受，请由用户拍板后走部署侧例外流程，"
                      "**不要**通过改高预算来绕过。",
              }, exit_code=2)
        return 2

    # ── 6) 提交（只有在还没拿到 task_id 时才真提交；否则上面已判定为续轮询）──
    if not task_id:
        status, resp = _http_json("POST", f"{GATEWAY_BASE}/media/generate", key, {
            "model": MODEL,
            "prompt": style,
            "params": {
                "mv": spec.get("mv", "chirp-v4-5"),
                "make_instrumental": "instrumental",
                "sample_rate": str(spec.get("sample_rate", 48000)),
                "bitrate": str(spec.get("bitrate", 192000)),
            },
        })
        if status == 402 or "insufficient" in json.dumps(resp).lower():
            emit({**plan, "ok": False, "cause": "bgm_insufficient_balance",
                  "error": "余额不足，已停止（不要重复提交）", "response": resp}, exit_code=2)
            return 2
        task_id = _find_task_id(_unwrap(resp)) or _find_task_id(resp)
        # ⭐ **先落盘、再判成败**：钱很可能已经扣了（真跑踩过：网关包了 `data`、
        #    task_id 是整数，我们却判成"提交失败"）。task_id 一丢，下次调用就会
        #    **重新提交 = 重复扣费** —— 落盘这一步比"把状态判准"重要得多。
        if task_id:
            write_json(project.bgm_task, {"task_id": task_id, "state": "submitted",
                                          "style": style, "output": out_rel})
        if status != 200 or not task_id:
            emit({**plan, "ok": False, "cause": "bgm_submit_failed",
                  "http": status, "response": resp,
                  "task_id": task_id,
                  "hint": (
                      "⚠️ HTTP 非 200，但已解析出 task_id 并**落盘到 bgm/task.json** —— "
                      "任务可能已创建、钱可能已扣。**再调一次会续轮询，不会重复提交**。"
                      if task_id else
                      "⚠️ 未拿到 task_id。请先核对响应体确认确实没创建任务，再重试。"
                  )},
                 exit_code=2)
            return 2

    # ── 7) 轮询 ──
    deadline = time.time() + poll_timeout
    last: dict = {}
    while time.time() < deadline:
        st, raw = _http_json(
            "GET", f"{GATEWAY_BASE}/skills/task-status?task_id={task_id}", key)
        last = _unwrap(raw)
        if st == 402 or "insufficient" in json.dumps(raw).lower():
            emit({**plan, "ok": False, "cause": "bgm_insufficient_balance",
                  "task_id": task_id, "response": raw}, exit_code=2)
            return 2
        # `is_final` 可能在外层或 data 里，两处都看
        if last.get("is_final") or raw.get("is_final"):
            last = {**last, **{k: v for k, v in raw.items() if k != "data"}}
            break
        time.sleep(5)
    else:
        write_json(project.bgm_task, {"task_id": task_id, "state": "polling",
                                      "style": style, "output": out_rel})
        emit({**plan, "ok": False, "cause": "bgm_timeout", "task_id": task_id,
              "hint": "任务仍在跑；再次调用本步会**续轮询**，不会重复扣费",
              "last_status": last}, exit_code=2)
        return 2

    state = str(last.get("state") or "").lower()
    result_url = _find_url(last)
    write_json(project.bgm_task, {"task_id": task_id, "state": state,
                                  "cost": last.get("cost"), "refunded": last.get("refunded"),
                                  "style": style, "output": out_rel})
    # 以「拿到可下载地址」为成功判据（state 用词各家不一：success/succeeded/done…）
    if not result_url:
        emit({**plan, "ok": False, "cause": "bgm_generate_failed", "task_id": task_id,
              "state": state, "response": last}, exit_code=2)
        return 2

    # ── 6) 下载 ──
    err = _download(result_url, output, key)
    if err:
        emit({**plan, "ok": False, "cause": "bgm_download_failed", "task_id": task_id,
              "result_url": result_url, "error": err}, exit_code=2)
        return 2

    size = output.stat().st_size
    report = {
        **plan, "task_id": task_id, "state": state, "resumed": resumed,
        "cost": last.get("cost"), "refunded": last.get("refunded"),
        "bytes": size,
        "checks": {"downloaded": size > 10_000},
        "next": "mix（edit/mix.json 的 bgm 指向本文件；长片记得 bgm_loop=true）",
    }
    write_json(project.bgm_report, report)
    emit(report, exit_code=0 if size > 10_000 else 2)
    return 0 if size > 10_000 else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段7 BGM 素材生成")
    ap.add_argument("--project", required=True)
    ap.add_argument("--spec", default="edit/bgm.json")
    ap.add_argument("--dry-run", action="store_true", help="只查价格并打印计划，不提交（不花钱）")
    ap.add_argument("--force", action="store_true",
                    help="已有 bgm 产物时仍重新生成（⛔ 会再扣一次费）")
    ap.add_argument("--confirm-paid", action="store_true",
                    help="确认这次要**真花钱**（约 0.49 元/次）；不带则只做免费检查并拒绝提交")
    ap.add_argument("--max-price", type=float, default=1.0, help="元/次上限，超过不提交")
    ap.add_argument("--timeout", type=int, default=900, help="轮询上限秒数")
    args = ap.parse_args()
    project = Project.resolve(args.project)
    project.ensure_dirs()
    spec = Path(args.spec)
    if not spec.is_absolute():
        spec = project.root / spec
    return cmd_generate(project, spec, dry_run=args.dry_run,
                        max_price=args.max_price, timeout=args.timeout,
                        force=args.force, confirm_paid=args.confirm_paid)


if __name__ == "__main__":
    guarded(main)
