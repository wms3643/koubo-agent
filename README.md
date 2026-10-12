# koubo-agent

口播视频剪辑的多代理编排项目。系统将剪辑、字幕、动效和质检中的内容判断交给专业代理，将转写、裁切、字幕重映射、渲染和媒体检查交给确定性工具执行。

仓库包含两套编排入口：`langgraph_app/` 提供 LangGraph 标准流程、对话主管和本地 Web 界面；`plugin/`、`agents/` 与 `profile/` 提供 DeepSeek Harness（DSH）集成。两者共用 `kb.py` 与 `pipeline/` 中的视频处理内核，支持本地项目管理与阶段续跑。

## 架构

```text
用户请求 / 命令行
       ↓
LangGraph 编排层或 DSH 主管
       ↓
剪辑代理 · 字幕代理 · 动效代理 · 质检代理
       ↓
kb.py → pipeline/ → FFmpeg / 转写模型 / HyperFrames
       ↓
项目目录中的决策文件、媒体产物和质检报告
```

LangGraph 标准流程通过检查点和失败原因路由支持暂停、恢复与回退。对话主管根据用户目标选择工具并委派专业任务。跨代理交接依靠项目文件和工具返回结果。详细实现见[架构说明](docs/architecture.md)与[交付契约](docs/contracts.md)。

## 目录

| 路径 | 用途 |
| --- | --- |
| `kb.py`、`pipeline/` | 视频处理命令行入口与确定性阶段 |
| `langgraph_app/` | LangGraph 编排、代理、状态与 Web 服务 |
| `plugin/`、`agents/`、`profile/` | DSH 插件及代理配置 |
| `skills/` | 代理执行时使用的领域参考 |
| `docs/` | 架构、契约与数据库设计资料 |

`projects/` 存放本地素材与运行产物，不纳入版本控制。仓库不包含样例视频、模型权重、网关密钥或已安装的第三方运行时。

## 运行要求

- Python 3.13，编排和 Web 服务依赖由 `requirements.txt` 固定版本；开发与测试依赖见 `requirements-dev.txt`。转写及动效依赖按处理阶段安装。
- FFmpeg 和 FFprobe 需位于 `PATH`；烧录字幕需要包含 `libass` 的 FFmpeg 构建。
- 转写需要可用的 `faster-whisper` 模型与计算环境；动效渲染需要 HyperFrames。
- 通过 `LK_API_KEY` 或 `YUANQI_API_KEY` 提供模型网关密钥；可用 `LK_BASE_URL`、`KB_TEXT_MODEL`、`KB_VISION_MODEL` 覆盖模型配置。
- 项目目录需位于 `KB_PROJECTS` 指向的根目录下，默认是仓库内的 `projects/`。可用 `KB_PYTHON` 指定视频内核运行的 Python 解释器。

在仓库根目录启动本地 Web 服务：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m langgraph_app.webapp.server
```

检查点与会话记录支持 PostgreSQL 持久化。设置 `DATABASE_URL` 后执行初始化，再启动应用：

```powershell
$env:DATABASE_URL = 'postgresql://koubo:your-password@127.0.0.1:5432/koubo'
python -m langgraph_app.database init
python -m langgraph_app.database check
python -m langgraph_app.webapp.server
```

以上 `python` 应使用已安装依赖的解释器。数据库容器、连接配置、备份和集成测试步骤见 [部署与运行](docs/deployment.md)。未设置 `DATABASE_URL` 时使用本地 SQLite；选用 PostgreSQL 后，连接或迁移错误会阻止启动。

默认监听 [http://127.0.0.1:8760/](http://127.0.0.1:8760/)。标准流程的命令行入口为：

```powershell
python -m langgraph_app.run projects/my-project
python -m langgraph_app.run projects/my-project --status
```

将 `my-project` 替换为实际项目目录名。流程遇到需要人工决定的事项会保存检查点并暂停；`--approve` 和 `--reject` 用于处理当前挂起事项。`--allow-paid` 会预先授权付费操作，使用前需确认预算。完整流程需要预先准备项目素材，不能仅凭仓库文件直接生成成片。

## 验证

流程测试使用替身内核与模型，不执行真实转写或视频渲染。存储集成测试连接真实 PostgreSQL，覆盖检查点恢复、人工审批续跑、会话持久化和删除：

```powershell
python -m pytest langgraph_app/tests -q -p no:cacheprovider
```

DSH 集成另有 `tools/` 下的自检脚本，运行它们需要对应的 Node.js、DSH 与媒体依赖。配置方式见 [DSH 子代理说明](agents/README.md)。媒体产物保存在项目目录；PostgreSQL 运行表及业务数据模型见 [数据库架构与实施规范](docs/postgres-design.md)。
