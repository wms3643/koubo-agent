"""langgraph_app 配置层。

设计原则（沿用本项目一贯做法）：
    1. **一切可配的都有默认值**，且默认值取自仓库内已有的事实源（不重复硬编码）；
    2. **密钥与路径不进代码** —— 只从环境变量读，缺了就 fail-loud；
    3. 本层不做任何判断性决策，只描述"系统长什么样"。

分工边界（重要）：
    `pipeline/` 是**确定性内核**（给定输入产出固定输出并自检）。
    本包是**编排层** —— 负责排程、判读 cause、裁决回退、在付费/高危动作前停下问人。
    两者通过 `kb.py` 的 CLI 契约交互（见 docs/contracts.md §一），**不互相 import 业务逻辑**。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================================
#  路径
# ============================================================================
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KB_ENTRY = REPO_ROOT / "kb.py"
DEFAULT_PROJECTS_ROOT = REPO_ROOT / "projects"

# 模型网关（元气中转站）——与 `profile/yuanqi-provider.patch.yml` 保持同一事实源
DEFAULT_BASE_URL = "https://api.lk888.ai/v1"
DEFAULT_TEXT_MODEL = "doubao-seed-2-0-pro-260215"
# 审片要看图 ⇒ 必须选一个多模态模型；与文本模型分开配置，便于单独替换
DEFAULT_VISION_MODEL = "doubao-seed-2-0-pro-260215"

# 密钥的查找顺序（与 pipeline/stage7_bgm.py 的 `_find_key` 约定一致，第三项是兜底）
KEY_ENV_NAMES = ("LK_API_KEY", "YUANQI_API_KEY")


def default_kernel_python() -> Path:
    """内核解释器。

    ⚠️ 为什么不能简单用 `sys.executable`：内核的转写阶段需要 **faster-whisper**，
    只有那一个专用解释器里装了（见 `pipeline/common.py` 顶部 `PYTHON_EXE`）。
    而本应用的 LLM 编排依赖（langgraph / langchain）在同一个解释器里也有，
    所以正常情况下两者就是同一个 —— 但仍然显式解析一次，避免"换了解释器就静默跑不起来"。

    解析顺序：`KB_PYTHON` > `pipeline.common.PYTHON_EXE` > 当前解释器。
    """
    env = os.environ.get("KB_PYTHON", "").strip()
    if env:
        return Path(env)
    try:
        # 只借用它的常量（纯路径，不含业务逻辑），避免两处各写一份本机路径
        from pipeline.common import PYTHON_EXE

        return Path(PYTHON_EXE)
    except Exception:  # noqa: BLE001 —— 内核不在解析路径上时退化为当前解释器
        return Path(sys.executable)


@dataclass(frozen=True)
class ModelConfig:
    """LLM 侧配置。`api_key` 为 None 时，构造客户端会 fail-loud。"""

    base_url: str = DEFAULT_BASE_URL
    text_model: str = DEFAULT_TEXT_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout_s: float = 180.0
    max_retries: int = 2

    @property
    def key_available(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class Budgets:
    """护栏预算 —— 数值全部来自 `docs/contracts.md` §三「护栏」。

    改这里必须同步那份契约，否则"文档说 5 次、代码放 8 次"这类漂移会重新出现。
    """

    # 单个阶段累计回退上限
    retake_budget: int = 5
    # ⛔ 跨 agent 回路每片只允许 1 次（改保留块 ⇒ 字幕全废、成片要重编码，贵一个数量级）
    cross_agent_loop_budget: int = 1
    # 同一 cause 连续未解决达到此次数 ⇒ 停止重试、向用户报告（**不是**再试一次）
    same_cause_limit: int = 2
    # BGM 单价上限（元/次）；超过则不提交
    bgm_max_price: float = 1.0


@dataclass(frozen=True)
class StageTimeouts:
    """按阶段分档的超时（秒）——取自 `docs/contracts.md` §一「超时（按阶段分档）」。

    ⚠️ 一个全局超时照顾不了两头：`qc` 卡死要尽快报出来，`transcribe` 又必须给足。
    """

    status: float = 2 * 60
    fast: float = 10 * 60          # cut-plan / srt / burn-test / qc
    bgm: float = 20 * 60           # bgm-generate
    encode: float = 30 * 60        # cut-apply / burn-full / assemble / mix / overlay-render
    transcribe: float = 60 * 60    # GPU 级


@dataclass(frozen=True)
class AppConfig:
    """一次运行的完整配置。用 `AppConfig.from_env()` 构造。"""

    kernel_python: Path = field(default_factory=default_kernel_python)
    kb_entry: Path = DEFAULT_KB_ENTRY
    projects_root: Path = DEFAULT_PROJECTS_ROOT
    model: ModelConfig = field(default_factory=ModelConfig)
    budgets: Budgets = field(default_factory=Budgets)
    timeouts: StageTimeouts = field(default_factory=StageTimeouts)
    # 应用自己的断点续跑数据库（与内核无关）
    checkpoint_db: Path = field(
        default_factory=lambda: REPO_ROOT / "langgraph_app" / ".state" / "checkpoints.sqlite"
    )

    @classmethod
    def from_env(cls, **overrides) -> "AppConfig":
        """从环境变量构造；显式 `overrides` 优先级最高（测试注入用）。

        环境变量：
            KB_PYTHON / KB_ENTRY / KB_PROJECTS       —— 内核侧
            LK_BASE_URL / LK_API_KEY / YUANQI_API_KEY —— 模型网关
            KB_TEXT_MODEL / KB_VISION_MODEL / KB_TEMPERATURE —— 模型选择
            KB_CHECKPOINT_DB                          —— 断点续跑库位置
        """
        env = os.environ.get
        key = next((env(n) for n in KEY_ENV_NAMES if env(n)), None)

        model = ModelConfig(
            base_url=env("LK_BASE_URL") or DEFAULT_BASE_URL,
            text_model=env("KB_TEXT_MODEL") or DEFAULT_TEXT_MODEL,
            vision_model=env("KB_VISION_MODEL") or DEFAULT_VISION_MODEL,
            api_key=key,
            temperature=float(env("KB_TEMPERATURE") or 0.2),
        )
        cfg = cls(
            kernel_python=Path(env("KB_PYTHON") or default_kernel_python()),
            kb_entry=Path(env("KB_ENTRY") or DEFAULT_KB_ENTRY),
            projects_root=Path(env("KB_PROJECTS") or DEFAULT_PROJECTS_ROOT),
            model=model,
            checkpoint_db=Path(env("KB_CHECKPOINT_DB")
                               or (REPO_ROOT / "langgraph_app" / ".state" / "checkpoints.sqlite")),
        )
        if not overrides:
            return cfg
        return cls(**{**cfg.__dict__, **overrides})

    # ------------------------------------------------------------------ #
    #  自检：把"跑之前就该发现的问题"在启动时报出来
    # ------------------------------------------------------------------ #
    def validate(self, *, require_model: bool = True) -> list[str]:
        """返回问题清单（空 = 一切就绪）。**不抛异常**，交由调用方决定怎么处理。"""
        problems: list[str] = []
        if not Path(self.kernel_python).is_file():
            problems.append(f"内核解释器不存在：{self.kernel_python}（设 KB_PYTHON）")
        if not Path(self.kb_entry).is_file():
            problems.append(f"内核入口不存在：{self.kb_entry}（设 KB_ENTRY）")
        if not Path(self.projects_root).is_dir():
            problems.append(f"项目根目录不存在：{self.projects_root}（设 KB_PROJECTS）")
        if require_model and not self.model.key_available:
            problems.append(
                f"找不到模型网关密钥：请设 {' / '.join(KEY_ENV_NAMES)}之一"
                "（密钥不落进任何配置文件）"
            )
        return problems

    def resolve_project_dir(self, project: str | os.PathLike[str]) -> Path:
        """把 `project` 归一化为绝对路径，并**断言它落在 `projects_root` 之内**。

        ⚠️ 为什么要有这个断言：DSH 那版把 `projectsRoot` 只当子进程 cwd，实际不做包含校验，
        而配置注释却写着"只允许操作它下面的项目" —— **承诺的边界不在代码里**（见 contracts.md
        D-4.13）。这一版把它做成真的。

        `projects_root` 本身允许被显式绕过：`KB_PROJECTS_ALLOW_ANY=1`（自测/临时项目用）。
        """
        p = Path(project).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if os.environ.get("KB_PROJECTS_ALLOW_ANY") == "1":
            return p
        root = Path(self.projects_root).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            raise ValueError(
                f"项目越界：{p} 不在 projects_root（{root}）之内。"
                "确需在别处工作请改 KB_PROJECTS；临时绕过可设 KB_PROJECTS_ALLOW_ANY=1"
            ) from None
        return p
