# B站视频总结工具 (Bili-summary)

这是一个基于 SenseVoiceSmall ASR 模型和 DeepSeek的 B 站视频内容总结工具。它可以自动提取视频音频、进行高精度转录，并生成结构化的 Markdown 总结。

## 🚀 功能特性

- **高效转录**：采用 SenseVoiceSmall ASR 模型，支持多种语言，转录速度极快。
- **智能总结**：集成大语言模型，提供清晰、专业的视频内容总结（支持所有兼容 OpenAI 接口的服务商）。需要自行配置api密钥和base url，默认使用 [DeepSeek 官方 API](https://platform.deepseek.com/) 的 `deepseek-v4-flash` 模型。
- **自动分段**：自动处理长视频，确保转录和总结的完整性。
- **桌面界面**：基于 PySide6 的原生 Windows 界面，启动后立即显示窗口；FunASR、Torch 和下载任务在独立后台引擎中运行，不会冻结界面。
- **后台预热**：程序启动时提前加载语音识别模型，点击“开始处理”后无需再等待核心组件首次初始化。
- **流式反馈**：实时显示下载、转录和 AI 总结进度，支持取消任务并打开结果文件。
- **内容理解**：目前仅能理解音频内容，不支持视频内的图像理解，但对于大部分口播视频已经够用了。

## 🛠️ 安装与配置

### 1. 克隆项目
```bash
git clone https://github.com/lxtc220/bili-summary.git
cd bili-summary
```

### 2. 安装依赖
建议使用虚拟环境：
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 环境依赖
- **FFmpeg**: 请确保系统已安装 FFmpeg。在 Windows 上，你可以将 `ffmpeg.exe` 放在项目根目录下的 `ffmpeg` 文件夹中，或者在 `.env` 文件中配置 `FFMPEG_PATH`。
- **yt-dlp**: 用于下载 B 站视频音频。
- **B 站 cookies**: 如果下载时遇到 `HTTP 412 Precondition Failed`，通常需要补充登录态。可以在 `.env` 中设置 `BILIBILI_COOKIE_FILE` 指向 cookies 文件，或设置 `BILIBILI_COOKIES_FROM_BROWSER=chrome` / `edge` / `firefox` 读取浏览器 cookies。

### 4. 配置环境变量
复制 `.env.example` 为 `.env` 并填写你的 API 信息：
```bash
cp .env.example .env
```
编辑 `.env` 文件，填入 AI 服务商提供的 API Key 和 Base URL。默认使用 [DeepSeek 官方 API](https://platform.deepseek.com/)（`https://api.deepseek.com`，模型 `deepseek-v4-flash`），如需换用其他兼容 OpenAI 接口的服务商（如魔搭 ModelScope、硅基流动等），同时修改 `LLM_BASE_URL` 和 `MODEL_ID` 即可。

如果你在下载 B 站音频时收到 `412 Precondition Failed`，建议补充这些可选配置：
```bash
BILIBILI_COOKIE_FILE=C:\path\to\bilibili_cookies.txt
# 或者
BILIBILI_COOKIES_FROM_BROWSER=chrome
```

## 📖 使用方法

双击启动脚本即可打开桌面程序：
```bash
# Windows
后台启动.vbs
# 或
start.bat

```

也可以直接运行：
```bash
python desktop_app.py
```

首次启动会在后台加载 FunASR 模型，窗口会先打开并显示预热状态。模型加载完成后，“开始处理”按钮才会启用；模型文件已存在时，后续启动会明显更快。

## 📂 项目结构

- `desktop_app.py`: PySide6 桌面界面，只负责交互和展示。
- `desktop_engine.py`: 后台处理引擎，负责预热 FunASR、下载、转录和 AI 总结。
- `bili_core.py`: 核心功能逻辑（音频下载、转录、总结）。
- `requirements.txt`: Python 依赖项。
- `model_cache/`: ASR 模型缓存目录（运行后自动创建）。
- `intermediate_files/`: 临时音频文件目录。
- `final_outputs/`: 最终总结输出目录。

## 📜 开源协议
本项目采用 [MIT License](LICENSE) 协议。
