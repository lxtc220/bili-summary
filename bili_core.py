import os
import sys
import re
import subprocess
import threading
import time
from pathlib import Path

# 配置信息 - 使用本地 ffmpeg
ffmpeg_path = os.path.join(os.path.dirname(__file__), "ffmpeg")
if ffmpeg_path not in os.environ["PATH"]:
    os.environ["PATH"] = f"{ffmpeg_path};{os.environ['PATH']}"

# 重要：funasr 必须在 bilibili_api / modelscope 之前 import。
# 在 Windows 上，若 bilibili_api(curl_cffi) 和 modelscope 先加载，
# 会改变某些共享 native DLL 的状态，导致后续 funasr 内部 torch.jit
# 编译 bicif_paraformer/cif_predictor.py 时触发 access violation（段错误）。
# 因此 funasr 在本模块顶层最先 import；bilibili_api / modelscope / openai
# 这些较重且启动非必需的依赖改为在各自函数内懒加载（首次调用时 funasr
# 必已就位，顺序约束不变），避免 web_ui / api 入口 import 本模块时被拖慢。
try:
    import funasr  # noqa: F401
except Exception:
    # 模块缺失等情况下不阻塞 bili_core 其他功能（如 LLM 总结）
    pass

import json
from dotenv import load_dotenv

# 加载环境变量
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# AI 模型配置 (支持所有兼容 OpenAI 接口的服务商)
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
MODEL_ID = os.environ.get("MODEL_ID", "deepseek-v4-flash")

# B站配置
DEFAULT_BILI_USER_AGENT = os.environ.get(
    "BILIBILI_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
DEFAULT_BILI_REFERER = os.environ.get("BILIBILI_REFERER", "https://www.bilibili.com/")
DEFAULT_BILI_ORIGIN = os.environ.get("BILIBILI_ORIGIN", "https://www.bilibili.com")
_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]p=(\d+)")

# 验证 API 密钥是否存在
if not LLM_API_KEY:
    print("警告: 未检测到 LLM_API_KEY 环境变量，AI 总结功能将不可用。", file=sys.stderr)


class LLMServiceError(Exception):
    """AI 服务调用失败，message 可直接展示给用户。"""


def _extract_error_code(error):
    """兼容 OpenAI SDK 与各类 OpenAI-compatible 服务商的错误结构。"""
    for attr in ("code", "type"):
        value = getattr(error, attr, None)
        if value:
            return str(value)

    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested_error = body.get("error")
        if isinstance(nested_error, dict):
            return str(nested_error.get("code") or nested_error.get("type") or "")

    return ""


def _format_llm_error(error: Exception) -> str:
    """格式化 LLM 错误信息，提供用户友好的提示。"""
    import httpx

    status_code = None
    raw_message = str(error)

    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
    if hasattr(error, "status_code"):
        status_code = error.status_code
    if hasattr(error, "code"):
        status_code = error.code

    if status_code == 401:
        return (
            "AI 总结失败：API Key 无效或已过期。请检查 .env 文件或环境变量中的 LLM_API_KEY 配置。"
        )

    if status_code == 404:
        return (
            "AI 总结失败：模型不存在或当前账号无权调用该模型。"
            f"请检查 .env 中的 MODEL_ID。当前 MODEL_ID={MODEL_ID}。"
        )

    suffix = f"（服务商返回：{raw_message}）" if raw_message else ""
    return f"AI 总结失败：调用 AI 服务时出错{suffix}"


def extract_bvid_and_p(url):
    """从URL中提取BV号和分集号"""
    p = 1

    if not url:
        return None, p

    page_match = _PAGE_RE.search(url)
    if page_match:
        try:
            p = int(page_match.group(1))
        except ValueError:
            p = 1

    bvid_match = _BVID_RE.search(url)
    if bvid_match:
        bvid = bvid_match.group(1)
        return "BV" + bvid[2:], p

    return None, p


def _resolve_bili_video_url(source_url, bvid, page=1):
    """尽量保留用户输入的原始链接；否则退回到标准 BV 页面链接。"""
    if source_url and source_url.startswith(("http://", "https://")):
        return source_url

    video_url = f"https://www.bilibili.com/video/{bvid}"
    if page > 1:
        video_url += f"?p={page}"
    return video_url


def _extend_yt_dlp_command(cmd):
    """为 yt-dlp 补充更像浏览器的请求头和可选 cookies。

    B 站 CDN 经常在下载途中断开连接（"N bytes read, M more expected"），
    这里通过分块下载（--http-chunk-size）让断点可续传，并提高重试次数，
    避免整段音频因单次连接中断而失败。
    """
    cmd.extend([
        "--no-check-certificate",
        "--no-update",
        "--retries", "10",
        "--fragment-retries", "10",
        "--extractor-retries", "10",
        # 分块下载：B 站 CDN 不稳定时只重传当前块，而不是整段重来
        "--http-chunk-size", "10485760",
        # 指定 buffer 大小，缓解长连接被服务端提前关闭的问题
        "--buffer-size", "16384",
        "--user-agent", DEFAULT_BILI_USER_AGENT,
        "--add-header", f"Referer: {DEFAULT_BILI_REFERER}",
        "--add-header", f"Origin: {DEFAULT_BILI_ORIGIN}",
    ])

    cookie_file = os.environ.get("BILIBILI_COOKIE_FILE")
    cookie_from_browser = os.environ.get("BILIBILI_COOKIES_FROM_BROWSER")

    if cookie_file:
        cmd.extend(["--cookies", cookie_file])
    elif cookie_from_browser:
        cmd.extend(["--cookies-from-browser", cookie_from_browser])

    return cmd


def download_paraformer_model(progress_callback=None):
    """下载Paraformer模型"""
    if progress_callback: progress_callback("正在下载Paraformer模型...")

    model_cache_dir = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic")
    os.makedirs(model_cache_dir, exist_ok=True)

    model_id = "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    target_dir = os.path.join(model_cache_dir, "paraformer-zh")

    if os.path.exists(target_dir):
        return target_dir

    try:
        # modelscope 懒加载：仅在需要下载模型时才 import（此时 funasr 已加载）
        from modelscope.hub.snapshot_download import snapshot_download
        model_dir = snapshot_download(
            model_id,
            cache_dir=model_cache_dir,
            revision="master"
        )

        import shutil
        shutil.copytree(model_dir, target_dir)
        return target_dir
    except Exception as e:
        raise Exception(f"下载模型失败: {e}")


def download_vad_model(progress_callback=None):
    """下载VAD模型（fsmn-vad，用于检测语音段并据此断句）"""
    if progress_callback: progress_callback("正在下载VAD模型...")

    model_cache_dir = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic")
    os.makedirs(model_cache_dir, exist_ok=True)

    model_id = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
    target_dir = os.path.join(model_cache_dir, "fsmn-vad")

    if os.path.exists(target_dir):
        return target_dir

    try:
        # modelscope 懒加载：仅在需要下载模型时才 import（此时 funasr 已加载）
        from modelscope.hub.snapshot_download import snapshot_download
        model_dir = snapshot_download(
            model_id,
            cache_dir=model_cache_dir,
            revision="master"
        )

        import shutil
        shutil.copytree(model_dir, target_dir)
        return target_dir
    except Exception as e:
        raise Exception(f"下载VAD模型失败: {e}")


def download_sensevoice_model(progress_callback=None):
    """
    下载SenseVoiceSmall模型。

    注意：必须放到纯英文路径下。sentencepiece 0.2.1+ 在 Windows 加载
    中文路径下的 bpe.model 会段错误（access violation），所以这里
    显式指定英文子目录名 'sense-voice'，而不是用 ModelScope 默认的
    含中文用户名的缓存目录。
    """
    if progress_callback: progress_callback("正在下载SenseVoice模型...")

    model_cache_dir = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic")
    os.makedirs(model_cache_dir, exist_ok=True)

    model_id = "iic/SenseVoiceSmall"
    target_dir = os.path.join(model_cache_dir, "sense-voice")

    if os.path.exists(target_dir):
        return target_dir

    try:
        # modelscope 懒加载：仅在需要下载模型时才 import（此时 funasr 已加载）
        from modelscope.hub.snapshot_download import snapshot_download
        model_dir = snapshot_download(
            model_id,
            cache_dir=model_cache_dir,
            revision="master"
        )

        import shutil
        shutil.copytree(model_dir, target_dir)
        return target_dir
    except Exception as e:
        raise Exception(f"下载SenseVoice模型失败: {e}")


# --- ASR 模型单例与异步预加载 ---
# 模型实例放模块级全局，所有 session 共享一份（不能放进 Streamlit 的
# session_state，否则每个浏览器 tab 会复制一份，显存会爆）。
# 进程存活期间常驻；触发销毁的唯一条件是 Streamlit server 进程退出
# （30 分钟无连接自动停机 / 停止程序.bat / 手动重启）。
_asr_model_instance = None
_asr_model_lock = threading.Lock()


def preload_asr_model(progress_callback=None):
    """
    加载 SenseVoice + fsmn-vad 组合模型到全局单例（双检锁，幂等）。

    可被两类调用方触发：
      1. web_ui.py 在用户首次访问网页时起的 daemon 线程（后台预热）；
      2. transcribe_audio() 里，若单例还没就绪则阻塞等待加载完成。
    无论被并发触发多少次，模型只会加载一次。
    """
    global _asr_model_instance

    # 第一次无锁快检：已就绪直接返回，避免每次转写都抢锁
    if _asr_model_instance is not None:
        return _asr_model_instance

    with _asr_model_lock:
        # 抢到锁后再查一次，防止两个线程同时通过第一次检查
        if _asr_model_instance is not None:
            return _asr_model_instance

        if progress_callback:
            progress_callback("正在初始化 ASR 引擎 (SenseVoice + fsmn-vad)...")

        try:
            import torch
            os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'

            from funasr import AutoModel
            import gc

            torch.cuda.empty_cache()
            gc.collect()

            device = "cuda" if torch.cuda.is_available() else "cpu"
            if progress_callback:
                progress_callback(f"使用设备: {device}")

            # 确保模型文件就位
            local_asr_path = os.path.join(
                os.path.dirname(__file__), "model_cache", "models", "iic", "sense-voice"
            )
            if not os.path.exists(local_asr_path):
                download_sensevoice_model(progress_callback)

            local_vad_path = os.path.join(
                os.path.dirname(__file__), "model_cache", "models", "iic", "fsmn-vad"
            )
            if not os.path.exists(local_vad_path):
                download_vad_model(progress_callback)

            # 加载 ASR + 自动挂载 VAD（funasr 内部完成 VAD→切段→ASR→拼接）
            if progress_callback:
                progress_callback("正在加载模型权重...")
            model = AutoModel(
                model=local_asr_path,
                vad_model=local_vad_path,
                vad_kwargs={"max_single_segment_time": 30000},
                trust_remote_code=False,
                device=device,
                disable_update=True,
            )

            _asr_model_instance = model
            if progress_callback:
                progress_callback(f"ASR 引擎已就绪 (设备: {device})")
            return _asr_model_instance

        except Exception as e:
            if progress_callback:
                progress_callback(f"ASR 引擎加载失败: {e}")
            raise


def get_asr_model_status():
    """
    返回 ASR 引擎当前状态（供 web_ui.py 在侧边栏展示进度用，非阻塞）。
      "ready"   - 已就绪，可立即转写
      "loading" - 正在后台加载中
      "idle"    - 尚未触发加载
    """
    if _asr_model_instance is not None:
        return "ready"
    # 锁被占用 = 正在加载；锁空闲 = 还没开始
    if _asr_model_lock.locked():
        return "loading"
    return "idle"


def get_video_info(bvid):
    """获取视频详细信息"""
    try:
        # bilibili_api 懒加载（此时 funasr 已由模块级导入先加载，保持
        # "funasr 先于 bilibili_api/modelscope"的顺序，规避 Windows 段错误）。
        # bilibili_api 默认用 curl_cffi 作为 HTTP 客户端，但本机上 curl_cffi 的
        # native libcurl 加载证书会报 "error setting certificate verify locations"
        # （curl: 77），导致所有调用 B站 API 的操作全部失败。改用 httpx 客户端
        # 绕开 curl_cffi，证书验证走 Python 标准库，问题消失。
        try:
            from bilibili_api.utils.network import select_client
            select_client("httpx")
        except Exception:
            # 老版本 bilibili_api 无 select_client，保持默认（curl_cffi）
            pass
        from bilibili_api import video, sync

        v = video.Video(bvid=bvid)
        info = sync(v.get_info())
        return {
            "title": info['title'],
            "desc": info['desc'],
            "pic": info['pic'],
            "owner": info['owner']['name'],
            "owner_face": info['owner']['face'],
            "duration": info['duration'],
            "pubdate": info['pubdate'],
            "stat": info['stat'],
            "pages": info.get('pages', [])
        }
    except Exception as e:
        raise Exception(f"获取视频信息失败: {e}")


# 模块级：下载互斥锁，按任务键 {bvid}_p{page} 区分。
# Streamlit 的 rerun（重复点击按钮 / 刷新页面 / autorefresh）可能让
# download_audio 被并发调用，多个 yt-dlp 写同一输出文件会互相破坏
# （.part 互踩），导致下载永远无法完成、页面卡死。锁保证同一任务
# 同时只有一个下载在跑；后到的在等锁期间，先到的已下载完成，文件
# 已存在 → 直接跳过下载返回。
_download_locks = {}
_download_locks_guard = threading.Lock()


def _get_download_lock(bvid, page):
    """按任务键获取（必要时创建）下载互斥锁。"""
    key = f"{bvid}_p{page}"
    with _download_locks_guard:
        lock = _download_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _download_locks[key] = lock
        return lock


def download_audio(bvid, page=1, progress_callback=None):
    """下载B站视频的音频（同一任务互斥，防并发写同一文件卡死）"""
    lock = _get_download_lock(bvid, page)
    with lock:
        return _do_download_audio(bvid, page, progress_callback)


def _do_download_audio(bvid, page, progress_callback):
    """download_audio 的实际实现（调用方需已持有该任务的互斥锁）。"""
    if progress_callback: progress_callback(f"正在下载视频音频 (BV: {bvid}, P: {page})...")

    os.makedirs("intermediate_files", exist_ok=True)

    try:
        info = get_video_info(bvid)
        title = info['title']

        if len(info['pages']) > 1:
            audio_path = os.path.join("intermediate_files", f"{bvid}_p{page}.mp3")
            cmd = ["yt-dlp", "--playlist-items", str(page), "-x", "--audio-format", "mp3", "-o", audio_path, f"https://www.bilibili.com/video/{bvid}"]
            if 0 < page <= len(info['pages']):
                title = f"{title} - {info['pages'][page-1]['part']}"
        else:
            audio_path = os.path.join("intermediate_files", f"{bvid}.mp3")
            cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", audio_path, f"https://www.bilibili.com/video/{bvid}"]

        cmd = _extend_yt_dlp_command(cmd)

        if not os.path.exists(audio_path):
            # 在 Windows 上隐藏子进程黑框
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0

            # B 站 CDN 经常不稳定，整条命令也重试几次；yt-dlp 自带 --continue 可断点续传
            last_err = None
            for attempt in range(1, 4):
                result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
                if result.returncode == 0:
                    break
                last_err = result.stderr
                if progress_callback:
                    progress_callback(f"下载失败，正在重试 ({attempt}/3)...")
                time.sleep(2 * attempt)
            else:
                raise Exception(f"音频下载失败: {last_err}")

        return title, audio_path
    except Exception as e:
        raise Exception(f"下载音频异常: {e}")


def transcribe_audio(audio_path, progress_callback=None):
    """
    使用 SenseVoiceSmall 模型将音频转换为文字（funasr 自动流水线版）。

    通过 AutoModel(vad_model=...) 让 funasr 内部自动完成
    "VAD 检测 → 按静音切段 → 逐段 ASR → 拼接" 的完整流水线，
    无需在 Python 侧手动切片。max_single_segment_time 限制单段最长
    30 秒，长音频会自动按 VAD 边界切短，避免显存溢出。

    模型实例由 preload_asr_model() 全局单例管理：首次调用时若单例尚未
    就绪会阻塞等待加载，之后就绪状态下直接复用（不再每次重新加载、
    也不再 del）。模型常驻显存，直到 Streamlit server 进程退出。

    SenseVoiceSmall 自带标点、语种/情感/事件标签（<|zh|><|NEUTRAL|><|BGM|>等）
    以及 ITN（数字归一化），因此无需额外 punc 模型即可得到带标点的转写结果，
    也无需再像 Paraformer 那样手动补句号。标签按设计保留，不做清洗。
    """
    try:
        # 复用全局单例；若后台预加载尚未完成，这里会阻塞等待
        model = preload_asr_model(progress_callback)

        # 整段音频直接交给 funasr，language=auto 自动识别语种，use_itn=True 开启 ITN
        if progress_callback:
            progress_callback("正在转写...")
        res = model.generate(
            input=audio_path,
            language="auto",
            use_itn=True,
        )

        if isinstance(res, list) and len(res) > 0:
            return res[0].get("text", "")
        return ""

    except Exception as e:
        raise Exception(f"音频转文字失败: {e}")


def summarize_content(title, text, progress_callback=None, enable_thinking=False):
    """使用 AI 模型总结内容（非流式）。

    enable_thinking: 是否启用 DeepSeek 思考模式（思维链推理）。默认关闭。
        - False: 关闭思考，响应快
        - True:  开启思考，回答更深入但更慢（思维链内容 reasoning_content 不返回/丢弃）
    """
    if not LLM_API_KEY:
        raise ValueError("请先在 .env 或环境变量中配置 LLM_API_KEY。")

    if progress_callback: progress_callback("正在调用AI模型进行总结...")

    from openai import OpenAI
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

    # DeepSeek 思考模式通过 extra_body 传递（OpenAI 标准 schema 无此字段）。
    # 官方格式为 {"thinking": {"type": "enabled"/"disabled"}}，注意 thinking 是嵌套对象。
    # V4 系列默认开启思考，必须显式传 type=disabled 才能关闭。
    extra_body = {"thinking": {"type": "enabled" if enable_thinking else "disabled"}}

    try:
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=[
                {"role": "system", "content": "你是一个专业的视频内容总结助手，请根据提供的视频标题和文字稿，对这个视频进行总结，格式需要使用简单的markdown格式，需要保证清晰易读。请注意：文字内容是通过视频音频转录来的，所以有可能有问题，如果遇到拼写偏差，请自行修正，并不要在总结内容中体现出来。"},
                {"role": "user", "content": f"视频标题：{title}\n\n文字稿：{text}"}
            ],
            stream=False,
            extra_body=extra_body,
        )
        return response.choices[0].message.content
    except Exception as e:
        raise LLMServiceError(_format_llm_error(e))


def summarize_content_stream(title, text, progress_callback=None, enable_thinking=False):
    """使用 AI 模型总结内容（流式输出）。

    enable_thinking: 是否启用 DeepSeek 思考模式。开启后流式响应里会先吐
        reasoning_content（思维链过程），再吐 content（最终总结）。这里只
        透传 content，思维链过程不展示（如需展示可读取 delta.reasoning_content）。
    """
    if not LLM_API_KEY:
        raise ValueError("请先在 .env 或环境变量中配置 LLM_API_KEY。")

    if progress_callback: progress_callback("正在调用AI模型进行总结...")

    from openai import OpenAI
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

    extra_body = {"thinking": {"type": "enabled" if enable_thinking else "disabled"}}

    try:
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=[
                {"role": "system", "content": "你是一个专业的视频内容总结助手，请根据提供的视频标题和文字稿，对这个视频进行总结，格式需要使用简单的markdown格式，需要保证清晰易读。请注意：文字内容是通过视频音频转录来的，所以有可能有问题，如果遇到拼写偏差，请自行修正，并不要在总结内容中体现出来。"},
                {"role": "user", "content": f"视频标题：{title}\n\n文字稿：{text}"}
            ],
            stream=True,
            extra_body=extra_body,
        )

        for chunk in response:
            # 只取 delta.content（最终总结），忽略 delta.reasoning_content（思维链过程）
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        raise LLMServiceError(_format_llm_error(e))


def save_transcription(bvid, title, text, p=1):
    """落盘单个视频的转录稿，按 BV 号 + 分集号命名。

    在转写完成（第 3 步）后立即调用，使后续 AI 总结即使失败，下次提交
    同一视频也能跳过「获取信息 / 下载 / 转录」三步直接重试总结。命名采用
    {bvid}_p{p} 作为缓存键，与 load_cached_transcription 配套；文件内容
    格式与 save_results 写出的转录稿完全一致，便于回读解析。
    """
    intermediate_dir = "intermediate_files"
    os.makedirs(intermediate_dir, exist_ok=True)

    video_url = _resolve_bili_video_url(None, bvid, p)
    cache_key = f"{bvid}_p{p}"
    txt_path = os.path.join(intermediate_dir, f"{cache_key}_transcription.txt")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"视频标题: {title}\n视频链接: {video_url}\n\n转录内容:\n\n{text}")

    return txt_path


def load_cached_transcription(bvid, p=1):
    """读取已缓存的转录稿，按 BV 号 + 分集号定位。

    返回 (title, text)；文件不存在或解析失败时返回 (None, None)。标题从
    文件头部「视频标题:」行解析，正文从「转录内容:」标记之后截取——这两
    个标记正是 save_transcription / save_results 写入的固定格式，自洽。
    """
    cache_key = f"{bvid}_p{p}"
    txt_path = os.path.join("intermediate_files", f"{cache_key}_transcription.txt")

    if not os.path.exists(txt_path):
        return None, None

    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None, None

    title = None
    # 标题取首行「视频标题:」之后的内容
    for line in content.splitlines():
        if line.startswith("视频标题:"):
            title = line[len("视频标题:"):].strip()
            break

    marker = "转录内容:\n"
    idx = content.find(marker)
    text = content[idx + len(marker):].strip() if idx != -1 else None

    if not text:
        return None, None

    return title, text


def save_results(bvid, title, text, summary, p=1):
    """保存结果并清理多余缓存"""
    intermediate_dir = "intermediate_files"
    output_dir = "final_outputs"
    os.makedirs(intermediate_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    cache_key = f"{bvid}_p{p}"
    video_url = _resolve_bili_video_url(None, bvid, p)

    # 转录稿复用 save_transcription，保证与 load_cached_transcription 的命名 / 格式一致
    txt_path = save_transcription(bvid, title, text, p)

    md_path = os.path.join(output_dir, f"{cache_key}_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n## 视频链接\n{video_url}\n\n## 内容总结\n{summary}")

    # 限制缓存目录大小为 30MB
    limit_directory_size(intermediate_dir, 30 * 1024 * 1024)

    return txt_path, md_path


def limit_directory_size(directory, max_size_bytes):
    """限制目录大小，如果超过则删除旧文件"""
    try:
        files = []
        for f in os.listdir(directory):
            path = os.path.join(directory, f)
            if os.path.isfile(path):
                files.append((path, os.path.getmtime(path), os.path.getsize(path)))

        # 按修改时间排序（从旧到新）
        files.sort(key=lambda x: x[1])

        current_size = sum(f[2] for f in files)
        while current_size > max_size_bytes and files:
            oldest_file_path, _, file_size = files.pop(0)
            os.remove(oldest_file_path)
            current_size -= file_size
            print(f"已删除旧缓存文件以释放空间: {oldest_file_path}")
    except Exception as e:
        print(f"清理缓存目录失败: {e}")
