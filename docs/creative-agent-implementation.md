# 自主创作 Agent 首版实现与验证

日期：2026-11-10。依据 [CREATIVE_AGENT_SPEC.md](../CREATIVE_AGENT_SPEC.md) v0.15，在 `agent开发` 分支实现。用户已在规格讨论后明确授权实施。

## 已接入

- 右侧“辅助创作 / 自主创作”切换；对话固定模式，分别恢复对话、输入草稿及素材选择，其他对话的活动 Agent 提供返回入口。
- 中间工作台的手动工具入口继续可用；自主面板打开时，手动操作归入项目默认辅助对话，不作为修改活动 Run 的旁路，也不自动扩大 Agent 的素材范围。
- 独立 `CreativeAgentRun / Step / Call`、完整 Responses 原生函数调用与结果配对。主模型自行决定讨论回复、创作路径、必要澄清、观察、返工和交付，没有增加路由模型或固定生成流程。
- 15 个工作台 tools：首次素材范围确认、项目背景、限定素材搜索与读取、媒体观察、能力目录和指引、按需预检、统一创作调用、任务读取和持久等待、版本化文档、可见计划、必要澄清、交付。
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
| 15 个 tools 及输入 Schema | `backend/app/creative_agent_schema.py` |
| 工作台与现有创作能力适配 | `backend/app/creative_agent_tools.py` |
| 统一素材范围与资源管理保护 | `backend/app/creative_agent_scope.py` |
| 证据核验、执行与返工计数 | `backend/app/creative_agent_policy.py` |
| PyAV / FFmpeg 观察准备 | `backend/app/creative_agent_media.py` |
| Run / 活动 / 澄清 / 控制 API | `backend/app/creative_agent_api.py` |
| 新模式指引与不可变 hash | `prompt_guides/autonomous/1.1.0/`（保留 1.0.0） |
| 数据迁移 | `backend/migrations/versions/d81a0c4e2100_creative_agent.py` |
| 执行预约状态迁移 | `backend/migrations/versions/e92a5b8c3100_agent_execution_reservations.py` |
| 自主助手面板 | `frontend/src/components/AutonomousAssistant.tsx` |
| 协议、权限与恢复测试 | `backend/tests/test_creative_agent.py` |
| 素材收窄、生产身份及返工边界测试 | `backend/tests/test_creative_agent_boundaries.py` |
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

## 素材收窄与返工归属补充（2026-11-10）

新 Run 初始素材权限为待确认。原有勾选和明确引用构成权限上限，但不会立即把素材名称、备注、缩略图或正文发给模型。同一个主 Agent 通过新增 `set_resource_scope` 将用户原文解释为 `available`、`selected_only`、`none` 或 `only` 策略；可用具体 ID 或用户原文中的完整名称包含/排除素材。后台先在原授权集合内定位，再固化范围，排除项优先。搜索、分页聚合、读取、观察、预检、Plan 创建、执行提交和 Worker 真实提交均遵守生效范围；模型不能用该工具重新打开已封闭的范围。普通无素材讨论可以直接回复。

自然语言的含义由同一个主 Agent 判断，后台负责结构化策略、原文引文、身份及权限上限校验，不新增独立路由模型或用关键词编排创作。名称不完整、重名或不在授权集合内时需要用户明确选择；不以名称解析为由开放全库。必要澄清中用户新选择的素材单独授权，不恢复先前排除的其他素材；当前 Run 自己的新成果照常可用。

生产身份从显示计划中分离：`update_plan.creation_targets` 在生成前声明媒体产物及必要中间结果的稳定 ID、含义、媒体类型和要求绑定。后台生成 `operation_key`；`invoke_creation.operation_id` 引用生产目标 ID，而非 `items[].id`。首次创建生产任务后固定生产定义，允许继续调整可见步骤、状态、计划说明、工具和参数。生产目标清单可以在生成前调整；执行后不能通过新增、改名或改变含义来创造免费重做入口。重复同一生产目标必须关联本目标的原产物证据，返工次数仍按稳定要求计数，确定未提交的名额释放规则保留。

无需修改数据库结构：生效策略、稳定生产定义与冻结标记保存在现有 JSON 字段，执行身份同时进入 Plan / Execution 的冻结元信息。旧 Run 缺少新字段时保留原权限和原操作 ID 兼容规则，不改写历史任务或交付事实。新指引发布为 1.1.0，原 1.0.0 文件保留。

针对性回归包括：首次模型请求不含素材元信息、原文收窄后搜索与直接读取受限、精确名称及排除优先、关键素材开关不能重开、无参考仍能使用新成果、范围回执幂等、必要澄清的单份授权、计划步骤改名、生产目标改名/换含义/换要求受限、原问题额度不能省略、证据不能挪用、恢复后稳定身份和旧版本兼容，以及独立首帧和视频目标照常连续执行。

真实模型素材验证：`scripts/validate_creative_agent.py --scope` 在独立工作区放入两份选中素材和一份带检测标记的关键素材。用户原文限定只用 `chosen.md`、排除 `excluded.md` 且不读取关键素材；3 次真实模型请求完成范围确认、搜索和普通回复，实际可用集合仅含 `chosen.md`，发送前检查确认关键素材标记未进入任何模型请求，生成执行为 0。报告：`.data/agent-validation/d2271030-295b-4075-9bfa-dc07fc7702a4/report.json`。

补充回归：11 项边界用例通过；完整 87 项回归通过（546.509 秒），最后补充的生产目标含义冻结与恢复断言专项通过。前端构建、Ruff 和差异空白检查通过。旧指引 1.0.0 未修改。

验证脚本另支持 `--resume <独立工作区目录>`，只接受 `.data/agent-validation` 的直接子目录，恢复已经存在的 Run / Task，不创建新的生成。验证等待期限覆盖生成自身超时；恢复仍沿用原 Execution 的冻结截止条件，不把重启当作延长生成时间的理由。

额外真实图片验证记录：`.data/agent-validation/25223d51-f855-4728-b055-229e1faac68a/report.json`。模型完成范围确认、能力读取、生产目标声明和唯一一次 Qwen 图片提交；验证进程中断后用同一工作区恢复，Run / Task / 稳定操作身份及已用次数保持。生成达到冻结的 900 秒限制后请求取消，未保存图片；Agent 的再次提交被本次 1 次执行上限拦截，随后标记需要处理，没有重复生成或冒充交付。本次只证明真实提交、恢复及限额停止行为，不记为图片成品闭环通过。

复现新增验证：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/validate_creative_agent.py --scope
$env:PYTHONPATH = 'backend;backend/tests'
.venv/Scripts/python.exe -X utf8 -m unittest test_creative_agent test_creative_agent_boundaries test_project_library test_public_access test_media_delivery test_task_identity test_task_media test_task_timing -q
```

## 项目背景版本核验（2026-11-13）

Run 启动时保存的 `goal_snapshot.project`（目标、风格、revision）是本次创作唯一背景，不再由后续提交静默采用项目的新目标。`preview_creation` 和 `invoke_creation` 在新建方案前比较当前项目的 goal/style；`plans.execute` 在创建 Task / Execution 的同一事务内再次比较，作为真正的提交边界。只改项目名称、或把编辑改回原值，虽然 revision 变化但背景相同，不会阻塞；缺少 `goal_snapshot.project` 的旧 Run 保持兼容。

冲突时返回 `project_background_changed`，附带冻结/当前 revision、变化字段和恢复说明；不创建 Plan / Task / Execution，不消耗创作执行名额。已经提交的任务继续执行，已有成果仍可按原目标检查和交付；只有新的创作提交被拒绝。`get_project_context` 通过 `project_background` 提供 `frozen_revision`、`current_revision`、`revision_changed` 和 `goal_or_style_changed`，但不返回新的目标或风格正文，避免模型把新背景混入旧任务。

代码落点：`backend/app/creative_agent_scope.py` 的背景快照/比较/提交检查，`backend/app/creative_agent_tools.py` 的预检与提交入口，`backend/app/plans.py` 的任务创建边界。自主指引发布 `1.3.0`，1.2.0 保留。回归新增 6 项：目标变化阻止新提交且不占名额、仅改名不阻止、Plan 创建后背景变化在任务边界被拒、上下文只报告漂移不泄露新值、旧 Run 兼容、已提交调用幂等返回原任务。完整自主创作回归通过。

### 重启后的真实验证（2026-11-13）

服务以 `.data/dev` 重启后，健康检查显示 ComfyUI 0.31.0 connected/compatible、11/11 工具 available、模型 `deepseek-v4.1-flash` 已配置；前端生产构建通过。

- 真实图片闭环：`validate_creative_agent.py --generate`，7 次模型请求、1 次生成，模型自主选择 `image.generate.qwen` 1.0.0（fast），交付 512×512 RGB PNG（213,408 字节，sha256 `38a5ad59…cf28`，seed 1394107443563926）；任务 `completed`、执行 `succeeded`、保存 `complete`，6 项要求均基于实际像素检查通过。报告：`.data/agent-validation/bd0a0378-6118-4fc7-b5dc-881c576c51a1/report.json`。
- 真实背景拦截：新增 `--background` 模式，在模型已经决定提交、执行开始前修改项目目标。5 次模型请求后 `invoke_creation` 返回 `project_background_changed`；Task / Execution 均为 0，执行名额未消耗；后续模型请求看到 `goal_or_style_changed=true`，按 `needs_attention` 说明并保留原目标。报告：`.data/agent-validation/8aad67a6-7a6e-47ef-91bc-7a17b11896d1/report.json`。
- 本次只验证图片与拦截路径，未运行视频生成；ComfyUI 队列保持空闲，视频验证留待后续单独执行。

复现：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/validate_creative_agent.py --background
.venv/Scripts/python.exe -X utf8 scripts/validate_creative_agent.py --generate
```

## 阶段成果与证据面板（2026-11-13）

- 运行列表与详情为最新 Run 和所有未结束 Run 附加 `scope_summary` 与 `documents`。`scope_summary` 基于同一个 `allowed_assets` 谓词统计指定、关键素材、本次成果、文档和排除数量，并提供最多 8 个来源示例；类别可能重叠，`available` 为去重总数。历史终态 Run 仍可单独读取详情获得相同字段。
- `/api/v1/tasks` 新增 `agent_run_id` 过滤，面板直接复用现有任务、执行、输入产物和中间产物解析，不新增表或迁移。工作文档来自 `write_document` 已完成的调用，与任务产物分开列出。
- 新增 `GET /agent-runs/{run_id}/calls/{call_id}`，按需返回单个工具调用的参数与结果；活动列表本身不携带大参数和完整提示词。
- 自主面板新增四块：本次可用素材（来源、数量、示例，可定位）、阶段成果（任务状态、候选/中间产物缩略图、文档查看/引用/下载）、执行与返工（每个要求的已用次数和记录）、活动明细（能力、生产目标、执行参数、任务/证据 ID、检查与原始记录）。Run 结束后任务和活动轮询停止。
- 回归新增 3 项：来源计数与排除、文档列表与计数、运行面板接口/任务过滤/调用详情；前端 `tsc -b && vite build` 通过，完整后端 126 项测试通过。
