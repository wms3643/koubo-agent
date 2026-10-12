# DSH 子代理配置

本目录保存 DeepSeek Harness（DSH）入口的主管提示词和子代理 patch。LangGraph 对话入口的角色与工具定义位于 `langgraph_app/agent/`，两套配置独立维护。

`subagents.patch.yml` 为每个专业角色配置一个 `@deepseek-ai/dsh-tool-subagent` 实例，通过 `toolName` 注册为主管可调用的工具。DSH 不会自动扫描本目录中的 Markdown 文件生成子代理；`director.md` 是运行时使用的主管系统提示词。

| 委派工具 | 角色 | 主要职责 |
| --- | --- | --- |
| `delegate_transcript_editor` | 剪辑策划 | 判断删留、执行裁切、建立剪后时间轴 |
| `delegate_subtitle_maker` | 字幕制作 | 文本修正、字幕生成、测试片与全片烧录 |
| `delegate_overlay_designer` | 动效设计 | 根据剪后时间轴设计、渲染并叠加动效 |
| `delegate_qc_inspector` | 成片质检 | 运行检查、分析报告与抽帧、反馈问题 |

各角色的工具范围由 `toolFilter.allow` 限定。`kb_override` 仅供主管使用。`toolFilter` 按工具名授权，不按产物路径授权；共享目录中的写入边界还需由插件、内核与文件约定维护。

## 产物交接

| 产物 | 主要责任角色 |
| --- | --- |
| `edit/edit_decisions.json`、`edit/cut_blocks.json`、`edit/timeline_map.json` | 剪辑策划 |
| `edit/subtitle_fixes.json`、`edit/subtitles/final.srt` | 字幕制作 |
| `overlays/index.html`、`overlays/renders/` | 动效设计 |
| `edit/qc_report.json`、抽帧证据 | 成片质检 |

字幕修正与剪辑决策分别写入 `subtitle_fixes.json` 和 `edit_decisions.json`。内核生成 SRT 时合并相应修正；字幕任务不得覆盖剪辑决策文件。字段及失败原因见[内核与产物契约](../docs/contracts.md)。

## 加载配置

先加载插件 patch 注册 `kb_*` 工具，再加载子代理 patch，以便实例化时校验工具白名单：

```powershell
dsh --profile web --patch profile/cordis.patch.yml --patch agents/subagents.patch.yml --dump-config
```

模型提供方配置由运行环境决定。`profile/yuanqi-provider.patch.yml` 是可选配置，使用前需配置对应网关与凭据。DSH、Node.js 依赖及媒体处理环境均需单独安装。
