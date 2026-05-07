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

import requests
import json
from bilibili_api import video, sync
from modelscope.hub.snapshot_download import snapshot_download
from openai import OpenAI
from dotenv import load_dotenv

# 加载环境变量
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# AI 模型配置 (支持所有兼容 OpenAI 接口的服务商)
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
MODEL_ID = os.environ.get("MODEL_ID", "deepseek-ai/DeepSeek-V3.2")

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
    """为 yt-dlp 补充更像浏览器的请求头和可选 cookies。"""
    cmd.extend([
        "--no-check-certificate",
        "--retries", "5",
        "--fragment-retries", "5",
        "--extractor-retries", "5",
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


def download_audio(bvid, page=1, progress_callback=None):
    """下载B站视频的音频"""
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

            result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
            if result.returncode != 0:
                raise Exception(f"音频下载失败: {result.stderr}")

        return title, audio_path
    except Exception as e:
        raise Exception(f"下载音频异常: {e}")


def split_audio_fixed(audio_path, segment_length_ms=300000):
    """
    按固定时间长度分割音频（无VAD检测，速度更快）

    参数:
        audio_path: 音频文件路径
        segment_length_ms: 每段长度（毫秒），默认5分钟

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


def transcribe_audio(audio_path, progress_callback=None):
    """使用Paraformer模型将音频分段转换为文字（GPU优化版）"""
    if progress_callback: progress_callback("正在加载Paraformer模型并分段转写...")

    try:
        import os
        os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'

        from funasr import AutoModel
        import torch
        import gc

        # 清理GPU缓存
        torch.cuda.empty_cache()
        gc.collect()

        # 使用GPU，如果不可用则回退到CPU
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if progress_callback:
            progress_callback(f"使用设备: {device}")

        # 检查模型是否存在
        local_model_path = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic", "paraformer-zh")
        if not os.path.exists(local_model_path):
            download_paraformer_model(progress_callback)

        # 加载模型
        if progress_callback: progress_callback("正在加载ASR模型...")
        model = AutoModel(
            model=local_model_path,
            trust_remote_code=True,
            device=device,
            disable_update=True,
        )

        # 分割音频（固定5分钟/段，优化内存）
        if progress_callback: progress_callback("正在分割音频...")
        segments = split_audio_fixed(audio_path, segment_length_ms=300000)

        if progress_callback: progress_callback(f"音频已分割为 {len(segments)} 段，开始逐段转录...")

        # 逐段转录
        all_texts = []
        for i, (start_ms, end_ms, segment_audio) in enumerate(segments):
            if progress_callback:
                progress_callback(f"转录第 {i+1}/{len(segments)} 段 ({start_ms//1000}s-{end_ms//1000}s)...")

            # 保存临时文件
            temp_path = os.path.join("intermediate_files", f"temp_segment_{i}.wav")
            segment_audio.export(temp_path, format="wav")

            # 转录，使用更小的批处理大小
            try:
                res = model.generate(input=temp_path, batch_size_s=30)
                if isinstance(res, list) and len(res) > 0:
                    text = res[0]["text"].replace(" ", "")
                    all_texts.append(text)
            except Exception as seg_e:
                if progress_callback:
                    progress_callback(f"第 {i+1} 段转录失败: {seg_e}")
            finally:
                # 清理临时文件
                if os.path.exists(temp_path):
                    os.remove(temp_path)

            # 每段转录后清理GPU缓存
            if device == "cuda":
                torch.cuda.empty_cache()

        # 释放模型资源
        del model
        torch.cuda.empty_cache()
        gc.collect()

        # 合并所有文本
        full_text = ""
        for text in all_texts:
            if text:
                if full_text and not full_text[-1] in "。！？；：":
                    full_text += "。"
                full_text += text

        if full_text and full_text[-1] not in "。！？；：":
            full_text += "。"

        return full_text

    except Exception as e:
        import torch
        import gc
        torch.cuda.empty_cache()
        gc.collect()
        raise Exception(f"音频转文字失败: {e}")


def summarize_content(title, text, progress_callback=None):
    """使用 AI 模型总结内容（非流式）"""
    if not LLM_API_KEY:
        raise ValueError("请先在 .env 或环境变量中配置 LLM_API_KEY。")

    if progress_callback: progress_callback("正在调用AI模型进行总结...")

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
        raise LLMServiceError(_format_llm_error(e))


def summarize_content_stream(title, text, progress_callback=None):
    """使用 AI 模型总结内容（流式输出）"""
    if not LLM_API_KEY:
        raise ValueError("请先在 .env 或环境变量中配置 LLM_API_KEY。")

    if progress_callback: progress_callback("正在调用AI模型进行总结...")

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
        raise LLMServiceError(_format_llm_error(e))


def save_results(bvid, title, text, summary, p=1):
    """保存结果并清理多余缓存"""
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:100]
    intermediate_dir = "intermediate_files"
    output_dir = "final_outputs"
    os.makedirs(intermediate_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    video_url = _resolve_bili_video_url(None, bvid, p)

    txt_path = os.path.join(intermediate_dir, f"{safe_title}_transcription.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"视频标题: {title}\n视频链接: {video_url}\n\n转录内容:\n\n{text}")

    md_path = os.path.join(output_dir, f"{safe_title}_summary.md")
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
