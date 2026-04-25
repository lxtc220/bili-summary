import os
import sys
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

# 配置信息 - 使用本地 ffmpeg
ffmpeg_path = os.environ.get("FFMPEG_PATH") or os.path.join(os.getcwd(), "ffmpeg")
if os.path.exists(ffmpeg_path) and ffmpeg_path not in os.environ["PATH"]:
    os.environ["PATH"] = f"{ffmpeg_path};{os.environ['PATH']}"

# 基础库
import requests
import json
import threading
import time
import subprocess
import re
from pathlib import Path
from bilibili_api import video, sync

# --- ASR 模型单例与异步加载 ---
_asr_model_instance = None
_model_lock = threading.Lock()

def preload_asr_model(progress_callback=None):
    """异步预加载 ASR 模型到全局变量"""
    global _asr_model_instance
    
    if _asr_model_instance is not None:
        return _asr_model_instance
        
    with _model_lock:
        # 双重检查锁
        if _asr_model_instance is not None:
            return _asr_model_instance
            
        if progress_callback: progress_callback("正在初始化 ASR 引擎...")
        
        try:
            import torch
            import gc
            from funasr import AutoModel
            
            os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
            torch.cuda.empty_cache()
            gc.collect()
            
            device = "cuda" if torch.cuda.is_available() else "cpu"
            
            local_model_path = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic", "SenseVoiceSmall")
            if not os.path.exists(local_model_path):
                download_asr_model(progress_callback)
            
            # 加载模型
            _asr_model_instance = AutoModel(
                model=local_model_path,
                trust_remote_code=True,
                device=device,
                disable_update=True,
                ncps=True,
                vad_model="fsmn-vad",
                punc_model="ct-punc",
            )
            
            if progress_callback: progress_callback("ASR 引擎已就绪")
            return _asr_model_instance
            
        except Exception as e:
            if progress_callback: progress_callback(f"ASR 引擎加载失败: {e}")
            raise e

# AI 模型配置 (支持所有兼容 OpenAI 接口的服务商，如 DeepSeek 官方, 火山引擎等)
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com") # 默认 DeepSeek 官方
MODEL_ID = os.environ.get("MODEL_ID", "deepseek-ai/DeepSeek-V3.2")
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
        return str(body.get("code") or body.get("type") or "")

    response = getattr(error, "response", None)
    if response is not None:
        try:
            payload = response.json()
            nested_error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(nested_error, dict):
                return str(nested_error.get("code") or nested_error.get("type") or "")
        except Exception:
            pass

    return ""


def _format_llm_error(error):
    status_code = getattr(error, "status_code", None)
    raw_message = str(error)

    quota_markers = ("insufficient_quota", "quota", "token-limit", "billing")
    is_quota_error = status_code == 429 or any(marker in raw_message.lower() for marker in quota_markers)
    if is_quota_error:
        return (
            "AI 总结失败：当前 AI 服务额度不足或触发限额。"
            f"请检查服务商账号余额/套餐用量，或在 .env 中更换 LLM_API_KEY、LLM_BASE_URL、MODEL_ID 后重试。"
            f"当前配置：MODEL_ID={MODEL_ID}，LLM_BASE_URL={LLM_BASE_URL}。"
        )

    auth_markers = ("invalid_api_key", "unauthorized", "401")
    if status_code == 401 or any(marker in raw_message.lower() for marker in auth_markers):
        return (
            "AI 总结失败：API Key 无效或没有访问权限。"
            "请检查 .env 中的 LLM_API_KEY、LLM_BASE_URL、MODEL_ID 是否匹配同一个服务商。"
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
    """为 yt-dlp 补充更像浏览器的请求头和可选 cookies。"""
    cmd.extend([
        "--no-check-certificate",
        "--retries", "5",
        "--fragment-retries", "5",
        "--extractor-retries", "5",
        "--user-agent", DEFAULT_BILI_USER_AGENT,
        "--add-header", f"Referer: {DEFAULT_BILI_REFERER}",
        "--add-header", f"Origin: {DEFAULT_BILI_ORIGIN}",
        "--add-header", "Accept-Language: zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    ])

    cookie_file = os.environ.get("BILIBILI_COOKIE_FILE", "").strip()
    if cookie_file:
        cookie_file = os.path.abspath(cookie_file)
        if os.path.exists(cookie_file):
            cmd.extend(["--cookies", cookie_file])

    cookies_from_browser = os.environ.get("BILIBILI_COOKIES_FROM_BROWSER", "").strip()
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])

    extra_args = os.environ.get("YTDLP_EXTRA_ARGS", "").strip()
    if extra_args:
        import shlex
        cmd.extend(shlex.split(extra_args))

    return cmd

def download_asr_model(progress_callback=None):
    """下载 SenseVoiceSmall ASR 模型"""
    if progress_callback: progress_callback("正在下载 SenseVoiceSmall 模型 (阿里巴巴达摩院最新多语言模型)...")
    
    model_cache_dir = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic")
    os.makedirs(model_cache_dir, exist_ok=True)
    
    # 使用 SenseVoiceSmall 模型
    model_id = "iic/SenseVoiceSmall"
    target_dir = os.path.join(model_cache_dir, "SenseVoiceSmall")
    
    if os.path.exists(target_dir):
        return target_dir
        
    try:
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
        raise Exception(f"下载 ASR 模型失败: {e}")

def get_video_info(bvid):
    """获取视频详细信息"""
    try:
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

def download_audio(bvid, page=1, progress_callback=None, source_url=None):
    """下载B站视频的音频"""
    if progress_callback: progress_callback(f"正在下载视频音频 (BV: {bvid}, P: {page})...")
    
    os.makedirs("intermediate_files", exist_ok=True)
    
    try:
        info = get_video_info(bvid)
        title = info['title']
        video_url = _resolve_bili_video_url(source_url, bvid, page)
        
        if len(info['pages']) > 1:
            audio_path = os.path.join("intermediate_files", f"{bvid}_p{page}.mp3")
            cmd = ["yt-dlp", "--playlist-items", str(page), "-x", "--audio-format", "mp3", "-o", audio_path, video_url]
            if 0 < page <= len(info['pages']):
                title = f"{title} - {info['pages'][page-1]['part']}"
        else:
            audio_path = os.path.join("intermediate_files", f"{bvid}.mp3")
            cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", audio_path, video_url]

        cmd = _extend_yt_dlp_command(cmd)

        if not os.path.exists(audio_path):
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
            
            result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                combined_output = "\n".join(part for part in [stderr, stdout] if part)
                if "HTTP Error 412" in combined_output or "Precondition Failed" in combined_output:
                    raise Exception(
                        "B站返回 412 Precondition Failed。通常需要登录态、cookies 或更完整的浏览器请求头。"
                        "请配置 BILIBILI_COOKIE_FILE 或 BILIBILI_COOKIES_FROM_BROWSER 后重试。"
                        + (f"\n{combined_output}" if combined_output else "")
                    )
                raise Exception(f"音频下载失败: {combined_output or '未知错误'}")
        
        return title, audio_path
    except Exception as e:
        raise Exception(f"下载音频异常: {e}")

def split_audio_fixed(audio_path, segment_length_ms=600000):
    """
    按固定时间长度分割音频（无VAD检测，速度更快）
    
    参数:
        audio_path: 音频文件路径
        segment_length_ms: 每段长度（毫秒），默认10分钟
    
    返回:
        segments: 音频段列表 [(start_ms, end_ms, audio_segment), ...]
    """
    from pydub import AudioSegment
    
    audio = AudioSegment.from_file(audio_path)
    segments = []
    
    for i in range(0, len(audio), segment_length_ms):
        end = min(i + segment_length_ms, len(audio))
        segments.append((i, end, audio[i:end]))
    
    return segments


def clean_transcription_text(text):
    """清理转录文本，保留 SenseVoice 标签但修复重复标点"""
    if not text:
        return ""
    # 1. 不再移除 SenseVoice 特殊标签 <|...|>，保留它们
    
    # 2. 移除多余空格（中文转录通常不需要空格）
    text = text.replace(" ", "")
    # 3. 修复重复的标点符号
    text = re.sub(r'[，,]+', '，', text)
    text = re.sub(r'[。.]+', '。', text)
    text = re.sub(r'[？?]+', '？', text)
    text = re.sub(r'[！!]+', '！', text)
    # 4. 修复标点组合错误
    text = re.sub(r'，。', '。', text)
    text = re.sub(r'。，', '。', text)
    return text.strip()

def transcribe_audio(audio_path, progress_callback=None):
    """使用 SenseVoiceSmall 模型转录音频，支持异步预加载的模型实例"""
    global _asr_model_instance
    
    try:
        # 1. 检查模型是否已由后台线程加载完成，若未完成则阻塞等待
        if _asr_model_instance is None:
            if progress_callback: progress_callback("⏳ ASR 引擎正在初始化/预热中，请稍候...")
            model = preload_asr_model(progress_callback)
        else:
            model = _asr_model_instance

        # 2. 执行转录逻辑
        if progress_callback: progress_callback("正在启动 ASR 引擎处理全量音频 (SenseVoice + VAD)...")
        
        # 直接对完整音频路径进行转录
        try:
            import torch
            # 使用内置 VAD 自动处理静音切分和长音频
            res = model.generate(
                input=audio_path, 
                cache={}, 
                language="auto", 
                use_itn=True,
                batch_size_s=120, # 增大批处理时长
                sample_rate=16000
            )
            
            if isinstance(res, list) and len(res) > 0:
                # 使用专门的清洗函数处理全文
                full_text = clean_transcription_text(res[0]["text"])
            else:
                full_text = ""
                
        except Exception as e:
            raise Exception(f"音频转录执行失败: {str(e)}") from e
        
        # 优化点：不要在此处删除模型单例，因为我们要复用它
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        # 规范化末尾标点
        if full_text and full_text[-1] not in "。！？；：":
            full_text += "。"
        
        return full_text
        
    except Exception as e:
        import torch
        import gc
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()
        raise Exception(f"音频转文字失败: {str(e)}") from e

def summarize_content(title, text, progress_callback=None):
    """使用 AI 模型总结内容（支持 OpenAI 接口，非流式）"""
    if not LLM_API_KEY:
        raise ValueError("请先在 .env 或环境变量中配置 LLM_API_KEY。")
        
    if progress_callback: progress_callback("正在调用AI模型进行总结...")
    
    from openai import OpenAI
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    
    try:
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=[
                {"role": "system", "content": "你是一个专业的视频内容总结助手，请根据提供的视频标题和文字稿，对这个视频进行总结，格式需要使用简单的markdown格式，需要保证清晰易读。请注意：文字内容是通过视频音频转录来的，所以有可能有问题，如果遇到拼写偏差，请自行修正，并不要在总结内容中体现出来。"},
                {"role": "user", "content": f"视频标题：{title}\n\n文字稿：{text}"}
            ],
            stream=False,
        )
        return response.choices[0].message.content
    except Exception as e:
        raise LLMServiceError(_format_llm_error(e)) from e

def summarize_content_stream(title, text, progress_callback=None):
    """使用 AI 模型总结内容（支持 OpenAI 接口，流式）"""
    if not LLM_API_KEY:
        raise ValueError("请先在 .env 或环境变量中配置 LLM_API_KEY。")
        
    if progress_callback: progress_callback("正在调用AI模型进行流式总结...")
    
    from openai import OpenAI
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    
    try:
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=[
                {"role": "system", "content": "你是一个专业的视频内容总结助手，请根据提供的视频标题和文字稿，对这个视频进行总结，格式需要使用简单的markdown格式，需要保证清晰易读。请注意：文字内容是通过视频音频转录来的，所以有可能有问题，如果遇到拼写偏差，请自行修正，并不要在总结内容中体现出来。"},
                {"role": "user", "content": f"视频标题：{title}\n\n文字稿：{text}"}
            ],
            stream=True,
        )
        
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        raise LLMServiceError(_format_llm_error(e)) from e


def save_transcription(bvid, title, text, p=1):
    """AI 总结失败时也保留已完成的转录稿。"""
    intermediate_dir = "intermediate_files"
    os.makedirs(intermediate_dir, exist_ok=True)

    video_url = f"https://www.bilibili.com/video/{bvid}" + (f"?p={p}" if p > 1 else "")
    cache_key = f"{bvid}_p{p}"
    txt_path = os.path.join(intermediate_dir, f"{cache_key}_transcription.txt")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"视频标题: {title}\n视频链接: {video_url}\n\n转录内容:\n\n{text}")

    return txt_path

def save_results(bvid, title, text, summary, p=1):
    """保存结果并清理多余缓存"""
    intermediate_dir = "intermediate_files"
    output_dir = "final_outputs"
    os.makedirs(intermediate_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    video_url = f"https://www.bilibili.com/video/{bvid}" + (f"?p={p}" if p > 1 else "")

    cache_key = f"{bvid}_p{p}"

    txt_path = save_transcription(bvid, title, text, p)

    md_path = os.path.join(output_dir, f"{cache_key}_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n## 视频链接\n{video_url}\n\n## 内容总结\n{summary}")

    limit_directory_size(intermediate_dir, 30 * 1024 * 1024)

    return txt_path, md_path

def load_cached_summary(bvid, p=1):
    """尝试加载已缓存的总结内容"""
    cache_key = f"{bvid}_p{p}"
    md_path = os.path.join("final_outputs", f"{cache_key}_summary.md")
    txt_path = os.path.join("intermediate_files", f"{cache_key}_transcription.txt")

    if os.path.exists(md_path):
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read()
            summary_start = content.find("## 内容总结\n")
            if summary_start != -1:
                summary = content[summary_start + len("## 内容总结\n"):].strip()
                return summary, txt_path if os.path.exists(txt_path) else None
        except Exception:
            pass
    return None, None

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
