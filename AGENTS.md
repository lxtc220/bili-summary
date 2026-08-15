# AGENTS.md

B站视频总结工具（Bili-summary）：yt-dlp 下载 B 站视频音频 → FunASR + SenseVoiceSmall 转录 → OpenAI 兼容 LLM 生成 Markdown 总结。Windows 优先（win32），所有注释、UI、用户可见错误均为中文。

## 目录结构

- `bili_core.py` — 核心逻辑：URL 解析、模型下载（ModelScope）、ASR 单例、音频下载、转录、总结、结果落盘。**绝大部分业务逻辑都在这里，修改前先读文件头部注释。**
- `web_ui.py` — NiceGUI 前端（事件驱动，无 rerun），只 import bili_core，不做业务逻辑。长任务在后台线程执行，页面用 ui.timer 渲染共享 TaskState。
- `api.py` — FastAPI 后端（备用入口，含 30s 心跳自动退出），同样复用 bili_core。
- `frontend/` — 静态前端（旧版），当前主入口是 web_ui.py。
- `desktop_app.py` / `desktop_engine.py` — 已放弃的 PySide6 桌面版源码（保留作历史参考，PyInstaller 打包配置已删）。
- `model_cache/models/iic/` — ASR 模型（gitignore，运行时自动下载）。
- `intermediate_files/` — 临时音频 + 转录缓存（`limit_directory_size` 限制 30MB）。
- `final_outputs/` — `{bvid}_p{p}_summary.md` 总结输出。
- `benchmark/`、`bench_sv.py`、`check_onnx_model.py` — 验证/基准脚本（可随意使用）。
- `bili_core_backup_sherpa.py` — 旧 sherpa 实现备份，**不要改它**，也不要与 bili_core.py 混淆。

## 运行命令

- 启动 Web UI：`python web_ui.py`（`start.bat` / `后台启动.vbs` 的入口，端口 8080，浏览器自动打开；`BILI_UI_HEADLESS=1` 关闭自动开浏览器）
- 启动 FastAPI：`uvicorn api:app`
- 停止：`停止程序.bat`（taskkill python.exe / pythonw.exe）
- 配置：`.env`（LLM_API_KEY / LLM_BASE_URL / MODEL_ID / FFMPEG_PATH / BILIBILI_COOKIE_FILE / BILIBILI_COOKIES_FROM_BROWSER）
- 无测试、无 lint/typecheck 配置，不要臆造测试命令。

## 关键陷阱（代码注释里踩过的坑，改动时不要破坏）

1. **import 顺序**：bili_core.py 必须**先 import funasr**（带 try/except），再 import bilibili_api / modelscope。Windows 上顺序颠倒会导致 funasr 的 torch.jit 编译段错误（access violation）。
2. **bilibili_api HTTP 客户端**：必须 `select_client("httpx")` 绕开 curl_cffi，否则报 curl:77 证书错误、B 站 API 全部失败。
3. **模型路径必须纯英文**：SenseVoice 放 `model_cache/models/iic/sense-voice`。sentencepiece 在 Windows 加载含中文路径的 bpe.model 会段错误。
4. **ASR 单例**：`_asr_model_instance` 是模块级全局（双检锁），进程内只加载一次，所有页面/客户端共享。UI 侧的任务状态（TaskState）可以每页一份，模型实例绝不能。状态查询用 `get_asr_model_status()`（ready/loading/idle）。
5. **DeepSeek 思考模式**：V4 默认开思考，必须显式传 `extra_body={"thinking": {"type": "enabled"/"disabled"}}`（嵌套 dict，OpenAI 标准 schema 没有此字段）。流式响应只透传 `delta.content`，丢弃 `reasoning_content`。
6. **转录缓存格式自洽**：`intermediate_files/{bvid}_p{p}_transcription.txt` 固定格式为 `视频标题: ...\n视频链接: ...\n\n转录内容:\n\n{text}`，`save_transcription` / `load_cached_transcription` / `save_results` 三者必须保持一致；缓存键统一 `{bvid}_p{p}`。
7. **B 站 412**：下载失败先提示配置 cookies（BILIBILI_COOKIE_FILE 或 BILIBILI_COOKIES_FROM_BROWSER）；yt-dlp 命令已带分块下载/重试参数，勿删。
8. **自动退出**：web_ui.py 用 NiceGUI 的 `app.on_connect/on_disconnect` 维护连接计数，约 10 分钟无浏览器连接时 `os._exit(0)`；api.py 有独立心跳（30s）。测试时注意。断线时页面会显示中文覆盖层（重新加载按钮 + 每 3 秒自动探测恢复），依赖 NiceGUI 的 `window.onNiceGuiDisconnect/onNiceGuiConnect` JS 钩子。
9. **无控制台启动必须重定向输出**：pythonw 在零句柄环境（VBS 隐藏启动）下 `sys.stdout/stderr` 为 None，NiceGUI/uvicorn 启动写日志会直接崩溃（表现为双击后毫无反应）。`后台启动.vbs` 通过 `cmd /c "... > runtime_logs\web_ui.log 2>&1"` 提供真实文件句柄，不要去掉重定向。另：`ui.timer` 回调必须 async，同步回调会阻断事件分发。

## 约定

- `progress_callback(message)` 贯穿下载→转录→总结全链路，UI 进度靠它驱动，新功能不要绕过。
- 用户可见错误信息用中文、单行，直接展示给用户（如 `LLMServiceError`、`_format_llm_error` 对 401/404 有专门提示）。
- 路径用相对项目根目录的字符串（`intermediate_files`、`final_outputs`），与既有代码一致。
- Windows 上调用子进程（yt-dlp）时用 `STARTUPINFO` 隐藏黑框，见 `download_audio`。
- 视频内容理解只限音频，无图像理解，README 有说明。
