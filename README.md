# 映作 · AIV Studio

映作是一个运行在 Windows 上的 ComfyUI 创作工作台。告诉它你想拍什么，Agent 会规划分镜、调用本机工作流生成镜头，再把镜头剪成短片。你也可以在「辅助创作」中逐步确认方案，单独打磨一张图或一个镜头。参考素材、分镜和成片按项目保存，方便回来继续改。

[观看示例](#自主创作示例) · [快速启动](#启动) · [已接入的工作流](#已接入的工作流)

## 自主创作示例

**《酒馆夜话》** 是一段 23.75 秒的有声竖屏短片。这是创作者在已有角色和酒馆场景的基础上，给 Agent 的一轮要求：

> 接下来，生成一个视频，三位主角围坐在这个酒馆的桌子前喝酒吃饭。其中骑士说“马上就到哥布林巢穴了，我们在这里休息好，明天就去进攻”，随后三人干杯后结束。

Agent 把这段要求拆成围桌吃饭、骑士说话、同伴举杯和碰杯收尾几个镜头。截图左侧是项目里保留的分镜和不同剪辑版本，中央画布打开的是最终剪辑。

[![《酒馆夜话》工作台截图，点击播放最终剪辑](docs/examples/tavern-workbench.png)][play]

**[▶ 播放《酒馆夜话》][play]** · [下载 MP4][download] · 480 × 864 · 24 fps · 有声

[play]: https://cdn.jsdelivr.net/gh/Zijim/AIV-Studio@eda5d039a9fb9eb81accde5eab5bca5567da42bb/docs/examples/tavern_film_optimized_v2.mp4
[download]: https://github.com/Zijim/AIV-Studio/raw/eda5d039a9fb9eb81accde5eab5bca5567da42bb/docs/examples/tavern_film_optimized_v2.mp4

## 启动

在 Windows 上安装 Git、Python 3.12、Node.js 24，启动本机 ComfyUI 0.31.0 或更新版本。映作默认连接 `http://127.0.0.1:8188`。根据你想做的作品，准备对应的模型：

| 第一次想做什么 | 需要准备 |
| --- | --- |
| 用手动工具生成一张图 | 下方列出的三个 Z-Image Turbo 模型。手动工具可直接使用，无需配置 API Key。 |
| 用对话制作一条短片 | DeepSeek 官方 API Key，以及所用视频工作流的节点和模型。DeepSeek 负责理解需求与规划，ComfyUI 在本机生成画面。 |

打开 PowerShell，运行：

```powershell
git clone https://github.com/Zijim/AIV-Studio.git
cd AIV-Studio
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

打开 [本地工作台](http://127.0.0.1:8765)，在左上角新建项目。以后启动只需运行 `start.ps1`，按 Ctrl+C 关闭服务。

### 生成第一张图

把三个模型放进 ComfyUI 的对应目录。模型文件可从 [ComfyUI 官方 Z-Image Turbo 教程](https://docs.comfy.org/zh/tutorials/image/z-image/z-image-turbo)下载。

| 模型文件 | ComfyUI 内的目录 |
| --- | --- |
| `z_image_turbo_bf16.safetensors` | `models/diffusion_models/` |
| `qwen_3_4b.safetensors` | `models/text_encoders/` |
| `ae.safetensors` | `models/vae/` |

在画布底部选「快速文生图」，输入「中世纪酒馆内景，烛光照亮木桌和石墙，写实电影风格」。宽高各填 768，候选数量选 1 张。保存方案，在辅助创作对话中点「确认生成」，完成后即可在项目素材库看到图片。

### 让 Agent 做一条短片

在「设置」保存 DeepSeek 官方 API Key，然后在创作助手中选择「自主创作」。已有角色或场景图时，可以从素材库选中它们，再描述成片目标。例如：

> 用选中的酒馆图作为场景参考，做一条约 8 秒的竖屏短片。先给烛光下的木桌一个全景，再切到两只酒杯相碰的特写，保留环境声。

对话中可以查看计划、工具调用和阶段成果。制作完成后，从对话或项目素材库打开成片。对话会将本轮文字和用于读图的参考图片发送给 DeepSeek。

## 已接入的工作流

| 我想做什么 | 可用工具 |
| --- | --- |
| 生成图片 | Z-Image Turbo 快速文生图、Qwen 精细文生图 |
| 修改或放大图片 | 单图编辑、参考图编辑、矩形选区局部编辑、SeedVR2 高清放大 |
| 生成视频 | 文字生成、图片作为首帧、角色或场景参考、指定首尾帧、仅指定尾帧 |
| 合成短片 | 从视频抽帧、按分镜顺序拼接镜头 |

[工作流与模型对照](docs/workbench-guide.md#已接入的工作流) · [工作流参数用途](docs/工作流参数用途对照表.md)

## 更多文档

- [工作台使用手册](docs/workbench-guide.md) · 素材管理、多对话、任务恢复、备份、模型预算和共享设置。
- [开发与本机验证](DEVELOPMENT.md) · 开发环境和验证记录。
- [自主创作实现](docs/creative-agent-implementation.md) · Agent 如何组织任务、调用工具并交付成果。
