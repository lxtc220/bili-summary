# B站视频总结工具 (Bili-S)

这是一个基于 SenseVoiceSmall ASR 模型和 DeepSeek的 B 站视频内容总结工具。它可以自动提取视频音频、进行高精度转录，并生成结构化的 Markdown 总结。

## 🚀 功能特性

- **高效转录**：采用阿里巴巴 SenseVoiceSmall ASR 模型，支持多种语言，转录速度极快。
- **智能总结**：集成 DeepSeek 等大语言模型，提供清晰、专业的视频内容总结（支持所有兼容 OpenAI 接口的服务商）。
- **自动分段**：自动处理长视频，确保转录和总结的完整性。
- **Web UI**：基于 Streamlit 的现代化网页界面，操作简单。
- **自动管理**：网页关闭后自动退出后台进程，节省系统资源。

## 🛠️ 安装与配置

### 1. 克隆项目
```bash
git clone https://github.com/your-repo/bili-s.git
cd bili-s
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

### 4. 配置环境变量
复制 `.env.example` 为 `.env` 并填写你的 API 信息：
```bash
cp .env.example .env
```
编辑 `.env` 文件，填入 AI 服务商提供的 API Key 和 Base URL。

## 📖 使用方法

直接运行启动脚本或使用 Python 启动：
```bash
# Windows
start.bat

# 或者直接使用 Python
streamlit run web_ui.py
```

## 📂 项目结构

- `web_ui.py`: Streamlit 网页界面。
- `bili_core.py`: 核心功能逻辑（音频下载、转录、总结）。
- `requirements.txt`: Python 依赖项。
- `model_cache/`: ASR 模型缓存目录（运行后自动创建）。
- `intermediate_files/`: 临时音频文件目录。
- `final_outputs/`: 最终总结输出目录。

## 📜 开源协议
本项目采用 [MIT License](LICENSE) 协议。
