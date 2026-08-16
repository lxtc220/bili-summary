# B站视频总结工具 (Bili-summary)

粘贴 B 站视频链接，自动完成：yt-dlp 下载音频 → 本地 FunASR + SenseVoiceSmall 转录 → OpenAI 兼容大模型（默认 DeepSeek）生成结构化 Markdown 总结。通过本地网页界面操作，全程无需命令行。

## 🚀 功能特性

- **网页界面**：基于 NiceGUI 的本地 Web UI（B 站粉主题，端口 8080），启动后自动打开浏览器，左右分栏布局，总结正文照搬 GitHub Markdown 排版。
- **本地转录**：SenseVoiceSmall 语音识别模型本地运行，首次使用自动从 ModelScope 下载；程序启动即在后台预热，进程内只加载一次，多页面共享。
- **智能总结**：支持所有兼容 OpenAI 接口的服务商（DeepSeek / 魔搭 / 硅基流动等），流式生成，需要自行配置 API 密钥。
- **转录缓存**：同一分 P 的转录结果自动缓存，重复处理同一视频时直接复用；临时文件目录限容 30MB 自动清理。
- **实时反馈**：下载 → 转录 → 总结全链路进度实时展示，支持中途取消任务、A4 排版打印总结。
- **自动退出**：约 10 分钟无浏览器连接自动退出后台进程；页面断线时显示重连覆盖层，服务恢复后一键重载。
- **内容理解**：仅分析音频内容，不支持视频画面识别，对绝大多数口播类视频已经够用。

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
venv\Scripts\activate   # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
```

### 3. 环境依赖
- **FFmpeg**：将 `ffmpeg.exe` 放在项目根目录的 `ffmpeg` 文件夹中，或在 `.env` 中配置 `FFMPEG_PATH`。
- **yt-dlp**：用于下载 B 站音频，已包含在 requirements.txt。
- **B 站 cookies（可选）**：下载遇到 `HTTP 412 Precondition Failed` 时需要补充登录态，见下方环境变量说明。

### 4. 配置环境变量
复制 `.env.example` 为 `.env` 并填写你的 API 信息：
```bash
cp .env.example .env
```
默认使用 [DeepSeek 官方 API](https://platform.deepseek.com/)（`https://api.deepseek.com`，模型 `deepseek-v4-flash`）。如需换用其他兼容 OpenAI 接口的服务商，同时修改 `LLM_BASE_URL` 和 `MODEL_ID` 即可，`.env.example` 中附有常用服务商示例。

如果下载 B 站音频时收到 `412 Precondition Failed`，建议补充这些可选配置：
```bash
BILIBILI_COOKIE_FILE=C:\path\to\bilibili_cookies.txt
# 或者
BILIBILI_COOKIES_FROM_BROWSER=chrome
```

## 📖 使用方法

双击启动脚本（推荐）：
- `start.bat` — 启动 Web UI 并自动打开浏览器。
- `后台启动.vbs` — 无黑框后台启动，日志写入 `runtime_logs/web_ui.log`，同样自动打开浏览器。
- `停止程序.bat` — 结束所有相关 Python 进程。

也可以命令行运行：
```bash
python web_ui.py   # 主入口，默认端口 8080
set BILI_UI_HEADLESS=1 && python web_ui.py   # 不自动打开浏览器
uvicorn api:app    # 备用入口：FastAPI 后端
```

首次启动会在后台下载并预热 ASR 模型，页面立即可用；模型就绪前提交的任务会等待加载完成后自动开始。

## 📂 项目结构

- `web_ui.py`：主入口，NiceGUI 网页前端（只做交互展示，长任务在后台线程执行）。
- `api.py`：FastAPI 后端（备用入口）。
- `bili_core.py`：核心逻辑（URL 解析、音频下载、ASR 转录、LLM 总结、结果落盘）。
- `frontend/`：旧版静态前端，已被 web_ui.py 取代。
- `desktop_app.py` / `desktop_engine.py`：已放弃的 PySide6 桌面版源码，仅作历史参考。
- `model_cache/`：ASR 模型缓存目录（运行时自动创建）。
- `intermediate_files/`：临时音频 + 转录缓存（自动限容 30MB）。
- `final_outputs/`：总结输出目录，文件名为 `{bvid}_p{p}_summary.md`。
- `benchmark/`：验证与基准测试脚本。

## 📜 开源协议

本项目采用 [MIT License](LICENSE) 协议。
