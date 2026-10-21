# 映作 · AIV Studio

面向本机 ComfyUI 的个人创作工作台。项目与素材保存在本机，DeepSeek 每轮准备一个方案，用户确认后才执行生成；完成后等待下一轮反馈。

## 启动

需要 Windows、Python 3.12、Node.js 24 和已运行的 ComfyUI 0.31.0 或更新版本。默认 ComfyUI 地址为 `http://127.0.0.1:8188`。安装过程中不改变 ComfyUI 环境。

```powershell
cd D:\Project\AIV_Agent2
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

打开 `http://127.0.0.1:8765`。已安装依赖后，只需运行 `start.ps1`，保留这个终端运行服务。按 Ctrl+C 可以安全关闭，也可以在另一个终端停止应用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

默认数据目录是 `%LOCALAPPDATA%\AIVAgent\data`。可复制 `config.example.toml` 为 `config.toml` 自定义路径、端口和连接地址；不要将密钥写入配置文件。设置页内的预算与资源参数存入数据库并立即生效。切换目录时先关闭服务，保留旧目录。

也可以创建桌面快捷方式：

```powershell
.\scripts\create_desktop_shortcut.ps1 -DataDirectory D:\Project\AIV_Agent2\.data\dev
```

上述命令沿用本机当前项目/素材目录；不带参数则使用配置的默认目录。先启动ComfyUI，再双击桌面“映作工作台”：服务在后台启动，就绪后自动用系统默认浏览器打开；重复双击会复用正在运行的服务。关闭网页不会停止后台服务，停止时给 `stop.ps1` 传入相同数据目录。启动日志保存在数据目录的 `logs` 中。

## 使用

1. 新建项目，填写创作目标和风格。
2. 导入文件、整个文件夹，或通过素材栏“导入ComfyUI既有产物”入口选择本机output目录内文件；Ctrl / Shift 点击可选择多份素材。每批文件显示独立导入结果。
3. 在设置中保存 **DeepSeek 官方 API Key**，默认模型 ID 是 `deepseek-flash`，对应 DeepSeek V4.1 Flash。也可在启动服务前设置 `DEEPSEEK_API_KEY`。环境变量优先于 Windows 凭据管理器。
4. 选择图片后，在输入框上方指定用途（自动判断、待编辑原图、主体/风格/构图参考、视频首帧），再发送需求。也可以直接粘贴当前项目图片的本工作台预览/原图链接，后台会自动附图。每轮最多6张，本轮选中或链接引用的图片会生成读图副本并发送至 DeepSeek；其他素材只提供文字信息。回复下方可展开“模型图片观察”，查看各图观察和不确定之处。
5. 助手在后台准备方案，可以放弃正在规划的回合；检查方案中的输入、提示词、候选数量、种子及实际视频时长，点击“确认执行”。不配置密钥时可以使用底部手动工具。
6. 结果自动归档；打开素材详情填写备注、标记最佳候选、查看来源关系。通过跨项目素材库复用已有素材。
7. 任务详情显示执行和保存状态。补存只取回已有输出；重做创建新方案，并再次确认执行。

同一项目可以开启多个独立对话：点击助手顶部“新对话”，通过对话名称旁的下拉列表切换、搜索、重命名或归档；在“已归档”中可恢复。新对话首次发送后自动命名。项目目标、风格与素材库共享，聊天记录、历史摘要、待确认方案、未发送草稿和所选附图各自独立；需要讨论其他对话的成果时，明确选择该素材或粘贴其本工作台链接。任务结果回到发起它的对话。已有聊天自动迁入“默认对话”，原始记录保留。

ComfyUI 中已有外部任务时，本应用等待其队列清空。候选串行运行，保留每个候选的执行、种子与产物。应用关闭不会停止已被 ComfyUI 受理的任务，下次启动通过 prompt ID 查询历史与队列；证据丢失时显示结果未知，不自动重复提交。

素材“移除”和项目“移除”都可以恢复，归档项目可以查看、搜索和复用。永久文件清理是设置页中的独立操作，先显示受影响素材与可释放大小，确认时再次核查引用。备份采用 SQLite backup API，包含原文件、工具版本和执行记录；恢复校验后写入独立目录，通过配置切换启用。密钥不进入备份。

## 已接入的工作流

| 工具 | 本机工作流 | 使用方式 |
| --- | --- | --- |
| 快速文生图 | 基础文生图.json | Z-Image Turbo，1–4候选 |
| 精细文生图 | 文生图qwen.json | Qwen Image，快速或质量 |
| 图片编辑 | 图片编辑.json | 单图快速编辑 |
| 参考图编辑 | 图片编辑qwen.json | 原图与参考图，质量分支 |
| 高清放大 | 图像高清放大.json | SeedVR2，检查实际最长边 |
| 图片生成视频 | video_minimax_h3_i2v.json | MiniMax H3本地，24fps，17k+5帧对齐 |

应用使用 `tool_registry` 中固定版本的 API 模板，不会修改原始工作流。`scripts/prepare_tools.py` 是开发时的导入工具，运行应用不需要 comfy-cli。

发布新工具版本时保留已注册的旧版本目录；运行时仅加载与应用内定义一致的版本，备份中的额外工具定义不会自动成为可调用工具。

## 开发

后端：FastAPI / Pydantic 2 / SQLAlchemy async / SQLite WAL / Alembic / HTTPX / websockets / Pillow / PyAV / keyring。前端：React 19 / TypeScript / Vite / Tailwind 4 / Radix / TanStack Query / Zustand / React Router / React Hook Form / Zod / React Flow。依赖分别锁定在 `backend/requirements.lock` 和 `frontend/package-lock.json`。

```powershell
$env:AIV_DATA_DIR = "$PWD\.data\dev"
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
# 在另一个终端
cd frontend
npm.cmd run dev
```

Vite 端口5173代理 `/api` 到后端。生产界面使用 `npm.cmd run build` 构建后由后端同源提供。后端只运行一个 worker，应用目录有进程锁。配置与数据库迁移在启动时完成，迁移前备份已有数据库。

本地 API 文档：`http://127.0.0.1:8765/docs`。写接口需要先 GET `/api/v1/session`，保留会话 cookie，设置同源 `Origin` 和响应中的 `X-AIV-CSRF`。这里只支持回环地址。

上下文使用随应用固定版本发布的 DeepSeek V4.1 Flash 官方分词器计数，并为供应商封装差异预留5%和512 tokens。每个对话独立使用默认128K输入、8K输出预算；达到96K时将较早对话整理成持久摘要，按token预算保留约24K的近期完整消息，不再按12条/2000字符截断。原始消息、摘要边界和每轮上下文快照保存在本机；每个对话独立整理，后续只处理摘要边界之后的新消息。界面可以加载更早消息，不受首屏条数限制。每日费用预算仍由整个工作台共用。

费用按2026-10-11核实的高峰单价估算，实际费用以 DeepSeek 官方账单为准。默认每日预算$0.50、每轮$0.15；方案生成最多2次，较早历史按最多64K输入分批整理，每轮至多4个整理请求，所有请求共用回合/每日费用预算。模型网络或认证错误不自动重试，未知用量继续占用预留预算。预算或整理失败时保留原始记录并明确报告，不静默丢弃历史。

读图使用原图生成内存中的 JPEG 副本：校正 EXIF 方向、最长边默认1536、不放大、透明区域按白底显示、动画仅取第一帧，原文件及 ComfyUI 输入不变；超过5000万像素或原文件缺失时明确报错。`vision_max_images`（默认6，上限12）和 `vision_max_edge`（默认1536，范围512–2048）可在 `[llm]` 配置中调整，重启生效。

链接入口支持本工作台 `127.0.0.1` / `localhost` 地址或相对路径的 `/api/v1/assets/<id>/preview`、`/content`。后台从本地素材库读取像素，不要求云端模型访问本机网址；链接素材必须属于当前项目且未移除，跨项目需先复用。选择与链接引用会去重，共用图片数量和费用预算；不会扫描旧消息自动附图，也不下载任意外部网址。

图片按[DeepSeek 官方视觉文档](https://api-docs.deepseek.com/guides/vision/)的每图最多1024 tokens预留，与文字共同受128K输入和费用限额约束；不会把base64当作文字分词。图片二进制只用于当轮请求，不重复写入调用日志；上下文和消息保存素材/原文件hash、发送尺寸、用途和模型观察，供后续对话及摘要延续。观察与用户要求、素材原有生成提示词分别保存。

主体参考不等于视频首帧。当前 MiniMax 工具从 `source_image` 整张画面开始；明确标为主体/风格/构图参考的本轮图片不能直接成为其首帧。需要其他开场构图时先讨论或准备首帧图片方案，确认后再推进视频。视频暂不读取连续帧，生成完成后也不会自动做视觉验收；想检查结果时，选中结果图片再发起讨论。

完整的模型联网闭环需要有效 DeepSeek 密钥。正式测试用例按此前约定后续补充，当前验证记录见 `DEVELOPMENT.md`。
