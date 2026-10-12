# 系统架构

## 组件边界

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| 视频处理内核 | `kb.py`、`pipeline/` | 执行确定性阶段，输出媒体产物与结构化报告 |
| LangGraph 标准流程 | `langgraph_app/graph.py`、`nodes/`、`routing.py` | 管理阶段状态、检查点、人工中断和失败路由 |
| 对话主管 | `langgraph_app/agent/director.py` | 根据用户目标选择工具并委派专业任务 |
| 专业代理 | `langgraph_app/agent/subagents.py` | 负责剪辑、字幕、动效和质检的领域判断 |
| 本地 Web 服务 | `langgraph_app/webapp/server.py` | 提供项目操作、对话、进度事件与产物访问 |
| DSH 集成 | `plugin/`、`agents/`、`profile/` | 通过插件工具和子代理配置接入视频内核 |

LangGraph 和 DSH 是两套独立编排入口，不共享会话状态。两者均通过 `kb.py` 调用 `pipeline/`，并在项目目录中读写产物。DSH 的工具配置仅约束 DSH 入口；LangGraph 入口有自己的工具与授权逻辑。

## 数据流

1. 项目素材进入 `projects/<项目名>/input/`。转写阶段生成带时间戳的逐字稿。
2. 剪辑任务读取逐字稿与证据文件，形成 `edit/edit_decisions.json`；内核据此裁切视频，生成 `edit/cut_blocks.json` 和 `edit/timeline_map.json`。
3. 字幕任务使用剪后时间轴生成 `edit/subtitles/final.srt`。编排流程在全片烧录前生成测试片和抽帧；内核 `burn-full` 命令本身未统一强制检查测试片是否存在。
4. 动效任务根据剪后时间轴生成透明叠加层。装配阶段合成视频、字幕、音频和动效产物。
5. 质检阶段检查媒体流、时长、字幕、黑帧和音频，输出 `edit/qc_report.json` 与抽帧证据。视觉问题仍需人工或视觉模型复核。

剪后时间轴是字幕与动效的共同输入。保留片段发生变化后，字幕时点、动效时点和后续渲染结果都需重新校验。读取同一项目的不同产物可以并行；写入同一文件或依赖尚未生成的时间轴时，必须按依赖顺序执行。

## 编排方式

`langgraph_app/run.py` 将标准流程编译为 LangGraph 图。节点通过 `Command` 路由下一步，`routing.py` 根据结构化 `cause` 选择继续、重试、回退或暂停。检查点支持 SQLite 和 PostgreSQL，同一 `thread_id` 可续跑并恢复人工中断。`DATABASE_URL` 控制后端选择，`langgraph_app/storage.py` 管理 PostgreSQL 连接、会话记录与初始化。

`langgraph_app/agent/director.py` 提供按需执行的对话入口。主管可查看项目、准备公共素材、处理跨角色装配任务并调用四个委派工具。每个委派工具启动一次独立的专业代理调用；子代理使用各自的工具白名单，其结论压缩后返回主管。委派结果需要结合工具返回与实际产物核对，不能仅凭自然语言结论认定成功。

DSH 入口使用 `plugin/src/index.js` 注册 `kb_*` 工具，`agents/subagents.patch.yml` 配置四个子代理。DSH 的 `toolFilter` 按工具名授权，不提供按文件路径隔离的能力。工具的参数及文件写入边界仍需由插件和内核校验。

## 失败处理与边界

内核命令输出 JSON 报告：退出码 `0` 表示成功，`2` 表示带 `cause` 的业务失败，`1` 表示运行异常。编排层按原因路由，并对单阶段回退、重复错误和跨代理回路设置上限。付费 BGM、强制重转写等操作需要显式授权或例外记录。

运行以项目目录保存素材与产物，以 SQLite 或 PostgreSQL 保存检查点。PostgreSQL 模式下，标准流程、对话记忆和会话列表分别存放于 `koubo_pipeline`、`koubo_chat` 与 `koubo_app` schema。Web 服务仍采用单进程运行，后台任务队列与运行锁在进程内维护；数据库后端的切换不改变任务并发策略。标准图中的字幕和动效分支按既定顺序推进。语义、审美和视觉缺陷需要结合抽帧证据进行复核。存储的表结构、事务边界、迁移步骤与验收标准见 [数据库架构与实施规范](postgres-design.md)。
