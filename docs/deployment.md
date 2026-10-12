# 部署与运行

## 环境准备

编排服务使用 Python 3.13。FFmpeg、FFprobe、转写模型和 HyperFrames 根据视频处理任务分别配置。模型网关密钥通过环境变量传入。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

后续命令中的 `python` 指该虚拟环境的解释器。仅启动服务时可安装 `requirements.txt`。

## PostgreSQL

仓库提供 PostgreSQL 16.6 的 Compose 配置，数据库端口绑定到本机回环地址。数据保存在命名卷中。启动前设置密码：

```powershell
$env:POSTGRES_PASSWORD = 'replace-with-a-local-password'
docker compose up -d --wait
$env:DATABASE_URL = "postgresql://koubo:$env:POSTGRES_PASSWORD@127.0.0.1:5432/koubo"
python -m langgraph_app.database init
python -m langgraph_app.database check
```

密码包含 URL 保留字符时，连接地址中的密码需进行百分号编码，也可以使用 Psycopg 支持的连接参数格式。`.env.example` 提供配置字段；Compose 可读取 `.env`，Python 应用读取进程环境变量。

`init` 创建运行表，并执行官方 PostgreSQL Saver 的迁移。重复执行保留已有记录，初始化期间通过数据库咨询锁避免并发迁移。常规请求不执行 DDL。

| schema | 数据 |
| --- | --- |
| `koubo_app` | 迁移版本、会话列表与界面事件的 JSONB 快照 |
| `koubo_pipeline` | 标准流程的原生 LangGraph 检查点、通道数据与待处理写入 |
| `koubo_chat` | 对话主管的原生检查点与模型记忆 |

会话快照以项目根目录的摘要区分工作区；同一部署应保持项目根目录路径稳定。检查点库按部署实例分配，两个独立服务不应复用同一组检查点 schema。

## Redis 检查点缓存

Compose 同时提供 Redis 7.4.2。启用 PostgreSQL 后，可设置以下环境变量缓存检查点快照：

```powershell
$env:REDIS_URL = 'redis://127.0.0.1:6379/0'
$env:KB_CHECKPOINT_CACHE_TTL = '60'
```

读取检查点时先查询 Redis，未命中则从 PostgreSQL 读取并缓存，默认 TTL 为 60 秒。缓存包含检查点、元数据和待处理写入，单条超过 2 MiB 时跳过缓存。历史列表仍直接查询 PostgreSQL。流程检查点与主管对话使用不同缓存命名空间。

检查点、待处理写入及删除操作始终提交 PostgreSQL，并在同一进程共享锁保护下提升缓存代数，使旧快照立即失效。失效后的键通过 TTL 清理；进程重启使用新的键前缀，避免重新使用旧缓存。Redis 连接、读取、写入或解码失败时，该进程停用对应缓存，后续读取 PostgreSQL；修复 Redis 后重启应用重新启用缓存。数据库写入错误仍会向调用方返回，不使用缓存掩盖持久化故障。

Redis 只承担缓存，关闭 RDB/AOF，内存上限为 128 MiB，采用 `allkeys-lru` 淘汰策略，不配置数据卷。缓存不纳入备份，丢失后按需重建。默认端口仅绑定本机；远程连接可使用带认证的 `rediss://` 地址。不设置 `REDIS_URL` 或选用 SQLite 时不启用缓存。

缓存遵循当前单进程部署约束：检查点读写必须通过应用的 Saver 接口，其他进程不能直接修改同一检查点 schema。它不提供多进程缓存一致性或任务分发。

## 服务启动

```powershell
$env:KB_PROJECTS = 'D:\koubo-projects'
$env:KB_PYTHON = 'D:\media-runtime\python.exe'
$env:LK_API_KEY = 'your-model-gateway-key'
python -m langgraph_app.webapp.server
```

项目根目录需预先创建，`KB_PYTHON` 需指向具备媒体依赖的解释器。默认监听 `127.0.0.1:8760`。`GET /api/health` 返回服务状态与存储后端；数据库不可达或缺少运行表时返回 503。PostgreSQL 模式下，启动检查失败会停止服务。

Web 服务使用一个进程，任务队列、对话对象和运行锁由该进程持有。部署时保持一个 Uvicorn worker。数据库保存恢复所需的检查点和会话历史；重启后的活动 SSE 连接需要重新建立，进行中的媒体子进程也需核对产物后恢复。付费授权位按会话运行周期管理，不在重启后自动恢复。

未设置 `DATABASE_URL` 时，标准流程检查点、对话记忆和会话列表分别保存在 `checkpoints.sqlite`、`chat.sqlite` 和 `conversations.json` 中。切换后端不自动复制这些数据，应保留原存储以便读取旧运行，或为新的数据库运行建立独立的会话和流程标识。

## 集成测试

```powershell
$env:KB_TEST_DATABASE_URL = $env:DATABASE_URL
$env:KB_TEST_REDIS_URL = $env:REDIS_URL
python -m pytest langgraph_app/tests -q -p no:cacheprovider
```

测试连接账号需要创建和删除测试数据库的权限。每轮创建名称带随机标识的独立测试库，结束后清理；不改动连接地址所指向数据库中的业务记录。未设置 `KB_TEST_DATABASE_URL` 时跳过 PostgreSQL 集成用例，SQLite 与流程回归照常执行。GitHub Actions 使用独立 PostgreSQL 服务运行全部存储用例。

Redis 集成测试同时需要 `KB_TEST_REDIS_URL`，使用随机线程标识，不执行全库清空。用例覆盖缓存命中、TTL 到期、待处理写入失效、跨连接删除、schema 隔离、缓存损坏降级，以及开启缓存时的付费审批恢复和 Web 会话恢复。CI 同时启动 PostgreSQL 与 Redis 服务。

## 备份与升级

备份覆盖三个运行 schema 及项目文件。Compose 环境可先在容器内生成 PostgreSQL 二进制备份，再复制到宿主机，避免 PowerShell 重定向改变二进制内容：

```powershell
docker compose exec -T postgres pg_dump -U koubo -d koubo -Fc -f /tmp/koubo-runtime.dump
docker compose cp postgres:/tmp/koubo-runtime.dump ./koubo-runtime.dump
```

备份文件包含会话历史，应放在受控目录。数据库恢复使用 `pg_restore`，同时恢复对应的项目文件；恢复后执行 `database check` 并验证已暂停流程能继续。升级前固定依赖版本、备份数据库，再执行 `database init` 与回归测试。删除 Compose 数据卷会删除数据库，应与普通停止服务区分。

部署账号按职责分配：迁移账号创建 schema 与表，应用账号获得运行表的读写和 schema 使用权限。外部访问由受控入口提供认证和 TLS；默认 Web 配置用于本机访问。
