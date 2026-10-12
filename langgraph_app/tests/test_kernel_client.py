"""`KernelClient` 的边界回归 —— 内核调用层是本应用最该被测透的一层。

覆盖的判读分支（每一条都对应一个真实会发生的失败形态）：

    A. 退出码语义：0 通过 / 2 业务性失败（带 cause）/ 1 环境异常
    B. stdout 里混进子进程噪声时**仍要**解析出报告（否则成功被误判成崩溃）
    C. 报告缺 `ok` 字段、或顶层不是对象时，退回按退出码定性
    D. `kb_qc` 的多因失败：`causes` 必须是复数（压成一个会选错回退目标）
    E. 超时：回带 `cause=timeout`，**并把 ffmpeg 等后代一起杀掉**，不能挂住
    F. 调用前就被拒（非法 device/mode/target、例外理由太短）⇒ 合成结果，不起进程
    G. 项目越界：`projects_root` 之外的路径必须被拒（承诺的边界要真的在代码里）

外加一条**真内核**集成冒烟：对 `projects/_smoke` 调 `kb.py status`。
它验证的是真实 CLI 契约 + 中文 UTF-8 编解码（正是"编码导致假崩溃"的高发处）。
"""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from langgraph_app.config import AppConfig, StageTimeouts  # noqa: E402
from langgraph_app.kernel import KernelClient, _extract_json_object  # noqa: E402

STUB = Path(__file__).parent / "stub_kernel.py"
SMOKE_PROJECT = REPO_ROOT / "projects" / "_smoke"


def make_config(tmp_path: Path, **overrides) -> AppConfig:
    """指向**桩内核**的配置（默认时限调小，让测试快）。"""
    base = AppConfig.from_env(
        kernel_python=Path(sys.executable),
        kb_entry=STUB,
        projects_root=tmp_path,
        checkpoint_db=tmp_path / "ck.sqlite",
        timeouts=StageTimeouts(status=10, fast=10, bgm=10, encode=10, transcribe=10),
    )
    return dataclasses.replace(base, **overrides) if overrides else base


@pytest.fixture()
def client(tmp_path) -> KernelClient:
    return KernelClient(make_config(tmp_path))


def run(client: KernelClient, scenario: str, monkeypatch, *, method="status", **kwargs):
    """按场景跑一次内核调用。`method` 是要调的工具方法名。"""
    monkeypatch.setenv("STUB_SCENARIO", scenario)
    fn = getattr(client, method)
    if method == "status":
        return fn(Path(client.config.projects_root))
    return fn(Path(client.config.projects_root), **kwargs)


# ---------------------------------------------------------------- A. 退出码语义
def test_ok_exit_zero(client, monkeypatch):
    r = run(client, "ok", monkeypatch)
    assert r.ok is True
    assert r.cause is None
    assert r.exit_code == 0
    assert r.report["stage"] == "stub"
    assert r.spawned is True
    assert r.duration_s >= 0


def test_business_failure_exit_two(client, monkeypatch):
    r = run(client, "business", monkeypatch, method="cut", run=True)
    assert r.ok is False
    assert r.exit_code == 2
    assert r.cause == "index_out_of_range"
    assert r.causes == ("index_out_of_range",)
    assert "越界" in r.report["error"]


def test_env_exception_exit_one(client, monkeypatch):
    r = run(client, "crash", monkeypatch)
    assert r.ok is False
    assert r.exit_code == 1
    assert r.cause == "kernel_exception"
    assert "traceback_tail" in r.report


# ------------------------------------------------- B. 噪声里也要取出报告
def test_noisy_stdout_still_parses(client, monkeypatch):
    r = run(client, "noisy", monkeypatch)
    assert r.ok is True, "子进程噪声不该让系统误判成崩溃"
    assert r.report.get("note") == "噪声之后的报告"


def test_garbage_with_exit_two(client, monkeypatch):
    r = run(client, "garbage", monkeypatch)
    assert r.ok is False
    assert r.cause == "kernel_business_failure"
    # 无报告可解析时，必须保留现场供人排查（否则只能看到"崩了"四个字）
    assert "不是 JSON" in r.stdout_tail


def test_garbage_with_exit_zero_is_ok(client, monkeypatch):
    r = run(client, "garbage_ok", monkeypatch)
    assert r.ok is True, "退出码 0 且无报告 ⇒ 按退出码定性为通过"
    assert r.cause is None


# ------------------------------------------- C. 报告字段缺失 / 形状不对
def test_missing_ok_field_falls_back_to_exit_code(client, monkeypatch):
    r = run(client, "no_ok_field", monkeypatch)
    assert r.ok is True
    assert r.cause is None


def test_top_level_array_treated_as_no_report(client, monkeypatch):
    r = run(client, "wrong_shape", monkeypatch)
    assert r.ok is False
    assert r.cause == "kernel_business_failure", "顶层是数组不算报告，应退回退出码定性"


# ------------------------------------------------------- D. 多因失败
def test_qc_multi_cause_is_plural(client, monkeypatch):
    r = run(client, "multi_cause", monkeypatch, method="qc", target="final")
    assert r.ok is False
    assert r.causes == ("srt_bom", "audio_clipping"), "多因失败不能被压成单个 cause"


# ------------------------------------------------------------ E. 超时
def test_timeout_kills_and_reports(client, monkeypatch):
    # 把快阶段时限压到 1 秒，桩会 sleep 60
    client = KernelClient(
        make_config(client.config.projects_root, timeouts=StageTimeouts(
            status=1, fast=1, bgm=1, encode=1, transcribe=1))
    )
    t0 = time.monotonic()
    r = run(client, "sleep", monkeypatch)
    elapsed = time.monotonic() - t0

    assert r.timed_out is True
    assert r.cause == "timeout"
    assert r.ok is False
    assert r.exit_code is None
    assert elapsed < 30, f"超时必须立刻返回，实际等了 {elapsed:.1f}s"


# ------------------------------------- F. 调用前就被拒（不起进程）
@pytest.mark.parametrize(
    "method,kwargs,cause",
    [
        ("transcribe", {"device": "tpu"}, "bad_spec"),
        ("burn", {"mode": "half"}, "bad_spec"),
        ("assemble", {"mode": "render", "spec": "edit/assemble.json"}, "bad_spec"),
        ("qc", {"target": "best"}, "bad_spec"),
        ("override_retranscribe", {"reason": "太短"}, "override_reason_required"),
        ("override_allow_hallucination_cut", {"reason": "  "}, "override_reason_required"),
    ],
)
def test_precall_rejection_never_spawns(client, monkeypatch, method, kwargs, cause):
    monkeypatch.setenv("STUB_SCENARIO", "ok")  # 若真起了进程，就会返回 ok
    r = getattr(client, method)(Path(client.config.projects_root), **kwargs)
    assert r.ok is False
    assert r.cause == cause
    assert r.spawned is False, "非法参数不该真的去起进程"
    assert r.exit_code is None


def test_override_accepts_valid_reason(client, monkeypatch):
    monkeypatch.setenv("STUB_SCENARIO", "ok")
    r = client.override_retranscribe(
        Path(client.config.projects_root), reason="逐字稿整段错位，用户已确认重转")
    assert r.spawned is True
    assert r.ok is True


def test_bgm_gate_flags_are_passed_through(client, monkeypatch):
    """`confirm_paid=True` 必须真的把两个开关都传给内核（否则等于没花钱也拿不到东西）。"""
    captured: dict = {}

    real_run = KernelClient._run

    def spy(self, **kwargs):
        captured.update(kwargs)
        return real_run(self, **kwargs)

    monkeypatch.setattr(KernelClient, "_run", spy)
    monkeypatch.setenv("STUB_SCENARIO", "ok")

    client.bgm(Path(client.config.projects_root), dry_run=True)
    assert captured["argv"] == ["bgm-generate", "--spec", "edit/bgm.json", "--dry-run"]

    client.bgm(Path(client.config.projects_root), confirm_paid=True)
    assert captured["argv"][:3] == ["bgm-generate", "--spec", "edit/bgm.json"]
    assert "--confirm-paid" in captured["argv"]
    assert "--max-price" in captured["argv"]
    # ⛔ --force 是刻意不暴露的：重复生成必须走"用户拍板"的人工路径
    assert "--force" not in captured["argv"]


# ------------------------------------------- G. 项目越界
def test_project_outside_root_is_rejected(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "inside").mkdir()
    assert cfg.resolve_project_dir(tmp_path / "inside") == (tmp_path / "inside").resolve()
    with pytest.raises(ValueError, match="越界"):
        cfg.resolve_project_dir(tmp_path.parent / "elsewhere")


def test_project_outside_root_can_be_explicitly_allowed(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setenv("KB_PROJECTS_ALLOW_ANY", "1")
    outside = tmp_path.parent / "elsewhere"
    assert cfg.resolve_project_dir(outside) == outside.resolve()


# ------------------------------------------- 纯函数：JSON 提取
@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"ok": true}', {"ok": True}),
        ('  {"ok": false, "cause": "x"}  ', {"ok": False, "cause": "x"}),
        ('noise\n{"ok": true}\nnoise', {"ok": True}),
        ("[1,2,3]", None),
        ("", None),
        ("   ", None),
        ("完全没有 JSON", None),
        ('{"ok": true', None),  # 截断的 JSON 不应被硬凑出来
    ],
)
def test_extract_json_object(text, expected):
    assert _extract_json_object(text) == expected


# ------------------------------------------- 真内核集成冒烟（跳过条件明确）
@pytest.mark.skipif(not SMOKE_PROJECT.is_dir(), reason="缺少 projects/_smoke 样例项目")
def test_real_kernel_status_on_smoke_project():
    """对**真内核**调一次只读命令，验证 CLI 契约与中文编解码。"""
    cfg = AppConfig.from_env(
        kernel_python=Path(sys.executable),
        kb_entry=REPO_ROOT / "kb.py",
        projects_root=REPO_ROOT / "projects",
        checkpoint_db=REPO_ROOT / "langgraph_app" / ".state" / "ck-test.sqlite",
        timeouts=StageTimeouts(status=120, fast=120, bgm=120, encode=120, transcribe=120),
    )
    r = KernelClient(cfg).status(SMOKE_PROJECT)
    assert r.ok is True, f"真内核 status 失败：{r.cause} / {r.stderr_tail[-300:]}"
    assert r.exit_code == 0
    assert "next_stage" in r.report
    assert "stages_done" in r.report
