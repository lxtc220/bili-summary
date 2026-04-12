import os
import sys

# 配置信息 - 使用本地 ffmpeg
ffmpeg_path = os.path.join(os.getcwd(), "ffmpeg")
if ffmpeg_path not in os.environ["PATH"]:
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

# 配置信息 - 火山引擎
VOLCANO_API_KEY = "c6793bb3-2de6-477a-b569-d75e9b31a0d4"
VOLCANO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MODEL_ID = "deepseek-v3-2-251201"
VIDEO_URL = "https://www.bilibili.com/video/BV1Ga4y1i77D/?spm_id_from=333.1387.homepage.video_card.click&vd_source=6cec98d87e21778c3c0afc1d666bf38b"

def extract_bvid_and_p(url):
    """从URL中提取BV号和分集号"""
    bvid = None
    p = 1
    
    if "BV" in url:
        bv_start = url.find("BV")
        # 找到问号或斜杠的位置
        question_mark = url.find("?")
        slash = url.find("/", bv_start + 2)
        end_pos = -1

        if question_mark != -1 and slash != -1:
            end_pos = min(question_mark, slash)
        elif question_mark != -1:
            end_pos = question_mark
        elif slash != -1:
            end_pos = slash

        if end_pos != -1:
            bvid = url[bv_start:end_pos]
        else:
            bvid = url[bv_start:]
            
        if "p=" in url:
            p_start = url.find("p=") + 2
            p_end = url.find("&", p_start)
            if p_end == -1:
                p_end = len(url)
            try:
                p = int(url[p_start:p_end])
            except ValueError:
                p = 1
    return bvid, p

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

def download_audio(bvid, page=1, progress_callback=None):
    """下载B站视频的音频"""
    if progress_callback: progress_callback(f"正在下载视频音频 (BV: {bvid}, P: {page})...")
    
    os.makedirs("intermediate_files", exist_ok=True)
    
    try:
        info = get_video_info(bvid)
        title = info['title']
        
        if len(info['pages']) > 1:
            audio_path = os.path.join("intermediate_files", f"{bvid}_p{page}.mp3")
            cmd = ["yt-dlp", "--playlist-items", str(page), "-x", "--audio-format", "mp3", "--no-check-certificate", "-o", audio_path, f"https://www.bilibili.com/video/{bvid}"]
            if 0 < page <= len(info['pages']):
                title = f"{title} - {info['pages'][page-1]['part']}"
        else:
            audio_path = os.path.join("intermediate_files", f"{bvid}.mp3")
            cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "--no-check-certificate", "-o", audio_path, f"https://www.bilibili.com/video/{bvid}"]

        if not os.path.exists(audio_path):
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
    """使用 SenseVoiceSmall 模型将音频分段转换为文字"""
    if progress_callback: progress_callback("正在加载 SenseVoiceSmall 模型并开始转录...")
    
    try:
        import os
        os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
        
        from funasr import AutoModel
        import torch
        import gc
        
        torch.cuda.empty_cache()
        gc.collect()
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        
        local_model_path = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic", "SenseVoiceSmall")
        if not os.path.exists(local_model_path):
            download_asr_model(progress_callback)
        
        # 加载 SenseVoice 模型
        model = AutoModel(
            model=local_model_path,
            trust_remote_code=True,
            device=device,
            disable_update=True,
            # SenseVoice 专用参数
            vad_model="fsmn-vad",
            punc_model="ct-punc",
        )
        
        # SenseVoice 本身对长音频支持较好，但分段转录能更细致地观察进度
        if progress_callback: progress_callback("正在分割音频以实现进度观察...")
        segments = split_audio_fixed(audio_path, segment_length_ms=300000)
        
        if progress_callback: progress_callback(f"音频已分割为 {len(segments)} 段，开始转录...")
        
        # 逐段转录
        all_texts = []
        for i, (start_ms, end_ms, segment_audio) in enumerate(segments):
            if progress_callback:
                progress_callback(f"转录第 {i+1}/{len(segments)} 段 ({start_ms//1000}s-{end_ms//1000}s)...")
            
            # 保存临时文件
            temp_path = os.path.join("intermediate_files", f"temp_segment_{i}.wav")
            segment_audio.export(temp_path, format="wav")
            
            # 转录
            try:
                # SenseVoice 默认输出包含情绪标签，例如 <|HAPPY|>, <|ZH|> 等
                res = model.generate(
                    input=temp_path, 
                    cache={}, 
                    language="auto", 
                    use_itn=True,
                    batch_size_s=60
                )
                if isinstance(res, list) and len(res) > 0:
                    # 使用专门的清洗函数
                    text = clean_transcription_text(res[0]["text"])
                    all_texts.append(text)
            except Exception as seg_e:
                if progress_callback:
                    progress_callback(f"第 {i+1} 段转录失败: {seg_e}")
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            
            if device == "cuda":
                torch.cuda.empty_cache()
        
        # 释放模型
        del model
        torch.cuda.empty_cache()
        gc.collect()
        
        # 合并并规范化标点
        full_text = "".join(all_texts)
        # 最后再对整体进行一次标点修复
        full_text = re.sub(r'[，,]+', '，', full_text)
        full_text = re.sub(r'[。.]+', '。', full_text)
        
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
    """使用火山引擎模型总结内容（非流式）"""
    if progress_callback: progress_callback("正在调用AI模型进行总结...")
    
    from openai import OpenAI
    client = OpenAI(base_url=VOLCANO_BASE_URL, api_key=VOLCANO_API_KEY)
    
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
        raise Exception(f"调用AI模型失败: {e}")

def summarize_content_stream(title, text, progress_callback=None):
    """使用火山引擎模型总结内容（流式输出）"""
    if progress_callback: progress_callback("正在调用AI模型进行总结...")
    
    from openai import OpenAI
    client = OpenAI(base_url=VOLCANO_BASE_URL, api_key=VOLCANO_API_KEY)
    
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
        raise Exception(f"调用AI模型失败: {e}")

def save_results(bvid, title, text, summary, p=1):
    """保存结果并清理多余缓存"""
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:100]
    intermediate_dir = "intermediate_files"
    output_dir = "final_outputs"
    os.makedirs(intermediate_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    video_url = f"https://www.bilibili.com/video/{bvid}" + (f"?p={p}" if p > 1 else "")
    
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
