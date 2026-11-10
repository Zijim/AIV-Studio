# 自主创作 Agent 首版实现与验证

日期：2026-11-10。依据 [CREATIVE_AGENT_SPEC.md](../CREATIVE_AGENT_SPEC.md) v0.15，在 `agent开发` 分支实现。用户已在规格讨论后明确授权实施。

## 已接入

- 右侧“辅助创作 / 自主创作”切换；对话固定模式，分别恢复对话、输入草稿及素材选择，其他对话的活动 Agent 提供返回入口。
- 中间工作台的手动工具入口继续可用；自主面板打开时，手动操作归入项目默认辅助对话，不作为修改活动 Run 的旁路，也不自动扩大 Agent 的素材范围。
- 独立 `CreativeAgentRun / Step / Call`、完整 Responses 原生函数调用与结果配对。主模型自行决定讨论回复、创作路径、必要澄清、观察、返工和交付，没有增加路由模型或固定生成流程。
- 14 个工作台 tools：项目背景、限定素材搜索与读取、媒体观察、能力目录和指引、按需预检、统一创作调用、任务读取和持久等待、版本化文档、可见计划、必要澄清、交付。
- 复用现有注册创作工具、参数 Schema、确定性提示词编译、Plan / Task / Execution、ComfyUI Worker、存储回执与 SSE。使用独立 `autonomous` 来源，单个生成结束不会替整个 Run 完成或要求人工反馈。
- 创建与澄清回答幂等，调用先持久化；创作提交的调用关联、执行预约和问题计数与 Task / Execution 在同一事务中提交。恢复先核对原身份，不重建未知生成。
- Worker 在排队结束、素材上传及真实提交前再次核验素材权限与取消状态。确定未提交的失败/取消释放预留名额；已受理、未知提交和有真实产物的尝试保留计数，释放本身有幂等状态与事件记录。
- 服务端在查询、分页、读取、图像准备和提交前约束素材范围：明确指定、项目关键素材、当前 Run 产物。无参考输入与关闭关键素材可用；不开放全库、跨项目扫描、Shell、外部检索或自动提升关键素材。
- 暂停、恢复、取消、等待、需要处理状态；必要澄清只能回答当前调用，不支持运行中自由更改目标。归档、移除和维护会检查活动 Run。
- 生成和返工限制、模型与工具调用数、无进展限制及推进时长。等待和暂停不计推进时长；已提交生成保留现有独立超时。服务运行时每秒保存推进计时，离线时间不计入。
- 不新增费用、token 消耗统计或 usage 汇总。新模式的供应商响应去掉 usage，保留必要协议、调用次数及上下文保护；旧模式既有逻辑继续保留。
- 正式 Markdown / JSON 文档复用 Asset / Blob，写新版本并关联父文档。成果支持查看、复制文档与下载。
- 自主模式生成的新成果不会自动变成下一次任务的素材授权；可通过“引用此成果”明确加入引用。关键素材开关按对话保存，切换模式不丢失。

## 代码落点

| 职责 | 文件 |
| --- | --- |
| 调度、原生协议、恢复、上下文与控制 | `backend/app/creative_agent.py` |
| 同对话讨论记录、按需读取与历史素材权限 | `backend/app/creative_agent_history.py` |
| 14 个 tools 及输入 Schema | `backend/app/creative_agent_schema.py` |
| 工作台与现有创作能力适配 | `backend/app/creative_agent_tools.py` |
| 统一素材范围与资源管理保护 | `backend/app/creative_agent_scope.py` |
| 证据核验、执行与返工计数 | `backend/app/creative_agent_policy.py` |
| PyAV / FFmpeg 观察准备 | `backend/app/creative_agent_media.py` |
| Run / 活动 / 澄清 / 控制 API | `backend/app/creative_agent_api.py` |
| 新模式指引与不可变 hash | `prompt_guides/autonomous/1.0.0/` |
| 数据迁移 | `backend/migrations/versions/d81a0c4e2100_creative_agent.py` |
| 执行预约状态迁移 | `backend/migrations/versions/e92a5b8c3100_agent_execution_reservations.py` |
| 自主助手面板 | `frontend/src/components/AutonomousAssistant.tsx` |
| 协议、权限与恢复测试 | `backend/tests/test_creative_agent.py` |
| 独立工作区真实验证 | `scripts/validate_creative_agent.py` |

迁移新增表和可空关联字段，旧对话默认为 `assisted`。保留原有启动迁移前备份流程；迁移包含冻结 DDL、模式兼容、活动对话唯一性和归属约束。启动时先恢复原存储与 Worker，再恢复自主 Run。

## 观察与能力边界

PyAV 提供实际解码、宽高、时长、音轨存在、带真实 PTS 的抽帧、时间区间及局部裁剪。默认一次最多 6 张图（沿用 `vision_max_images`），受媒体处理时间与解码帧数保护；未完整检查会返回 partial/failed，不冒充完整解码。

FFmpeg 在 PATH 可用时通过固定滤镜准备黑场、冻结、静音候选区间；模型不能传任意路径、滤镜或命令。当前开发机器未在 PATH 找到 FFmpeg，因此这些检测会明确返回“未执行”，PyAV 解码与抽帧照常可用。将 FFmpeg 加入运行环境 PATH 后可启用对应检测；未实测的检测效果不作为已验证结论。

DeepSeek 通过真实图片内容块读取观察结果，不只接收本地 URL。读取、观察和交付核验原文件实际 hash；检查证据绑定素材、hash、采样/缩放/裁剪和实际覆盖。提交检查时必须引用工具实际返回的 `data.evidence_call_id`，不能猜测标识。交付前验证文件、类型、保存状态、未闭合调用、任务未知状态和必要证据；关键不确定进入需要处理。

抽帧不能保证复杂动作、瞬时变形、物理、口型或声音语义已验证。首版不增加额外视频评估模型，也不接入新的剪辑、独立配音、混音、字幕合成或通用脚本生产能力。真实图像闭环通过不等于所有视频目标的质量已验证。

同一目标要求的问题使用稳定 requirement ID 归并，防止换措辞、工具、种子或 Task 重置返工次数；首版对同一要求下的多个问题采用较保守的合并计数。是否确实属于明确目标偏差仍依赖主模型对证据的解释，后端负责证据身份、协议及限额，不将其描述为独立语义裁判。

上下文采用保留完整调用/结果块的确定性整理，保留当前原始目标、澄清、计划、任务事实、资源引用和计数；较早正文可通过授权资源 ID 重新读取。未引入精确消费 token 计算，也不使用供应商会话 ID 恢复。

同一对话的普通讨论回复可作为后续语义上下文；创建快照和后续读取都核验其曾使用的素材范围，不能通过旧回复恢复被撤回的素材描述。保留最近完整记录，较长记录通过 `get_project_context(history_offset=..., history_limit=1)` 按整条读取，不截断本次原始目标。每轮回放也核验初始上下文中的素材引用。无进展判断忽略变化的调用 ID 和自增计数，重复读取相同事实不会获得新推进轮次。用户处理网络等问题后明确恢复，可重新开始一组有限请求重试，但 Run 累计请求数及其他限额不重置。

## 配置与使用

重启后端后按现有机制执行迁移，刷新前端，在右侧切到“自主创作”即可开始。现有模型凭据、模型名称和 ComfyUI 配置继续使用。

工程初值可通过环境变量或 `config.toml` 的 `[agent]` 段调整，无需新增设置面板：

```toml
[agent]
max_executions = 12
max_issue_reworks = 2
max_model_requests = 40
max_tool_calls = 120
max_no_progress = 3
active_seconds = 1800
media_seconds = 60
media_max_frames = 1800
```

每个 Run 固定启动时限额。环境变量对应 `AIV_AGENT_MAX_EXECUTIONS` 等名称。正常生成仍沿用现有冻结的任务超时与资源上限。

真实验证脚本默认写一份短文档；`--generate` 在 ComfyUI 空闲时验证一个图片候选，使用已配置的真实模型和 GPU。它创建独立 `.data/agent-validation/<uuid>/` 工作区，不读取用户项目库，保留报告、任务和产物用于核查：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/validate_creative_agent.py
.venv/Scripts/python.exe -X utf8 scripts/validate_creative_agent.py --generate
```

## 验证记录

- 真实 DeepSeek 文档闭环：写作、保存、读回全文、检查并自动交付；4 次模型请求，0 次生成执行。报告：`.data/agent-validation/4d0f3777-ac85-4744-9f41-c676301fb1c4/report.json`。
- 真实 DeepSeek + ComfyUI 图片闭环：模型自主选择 Qwen，生成一个 512×512 橙色纸船候选，等待保存、取得实际像素、逐项检查后交付；8 次模型请求，1 次生成执行。一次混合控制批次被拒后模型自行修正，没有重复生成。报告：`.data/agent-validation/df8fe479-7bcb-488e-829f-a836de8aff1d/report.json`。
- 验证中发现并修正了文档证据 ID 未返回、操作 ID 约定不清两项接入问题。后续以实际工具返回的 ID 和明确 Schema 完成闭环，不以模拟响应替代真实测试。
- PyAV 使用本地合成视频验证完整解码、真实时间戳、局部区间、裁剪和缺少 FFmpeg 时的能力说明；未执行真实模型/GPU 视频质量评估。
- 浏览器验证双模式独立对话、切回恢复草稿、关键素材开关持久保存、成果预览与显式引用。成果展示样例复用上面真实生成的图片，只验证 UI，不代表另一次生成；独立测试工作区截图位于 `.data/agent-ui-validation/autonomous-panel.png`。
- 最终回归：76 项测试全部通过（228.818 秒），涵盖新 Agent 与原项目库、公开访问、媒体交付、任务身份、素材和计时行为；前端 `npm run build`、新增 Python 文件 Ruff 检查和 `git diff --check` 通过。构建保留既有依赖注释与大包提示。
- 固定模型响应测试用于协议、权限、恢复及计数，不作为真实模型创作质量的证明。复现自动测试：

```powershell
$env:PYTHONPATH = 'backend;backend/tests'
.venv/Scripts/python.exe -X utf8 -m unittest test_creative_agent test_project_library test_public_access test_media_delivery test_task_identity test_task_media test_task_timing -q
```
