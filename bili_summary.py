import os
import sys
import requests
import json
import threading
import time
from bilibili_api import video, sync
import subprocess
from pathlib import Path
from modelscope.hub.snapshot_download import snapshot_download
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
from tqdm import tqdm



# 要处理的B站视频链接
VIDEO_URL = "https://www.bilibili.com/video/BV1At4y187H4/?spm_id_from=333.1387.homepage.video_card.click&vd_source=6cec98d87e21778c3c0afc1d666bf38b"

# 设置ffmpeg路径
FFMPEG_PATH = "G:\\ffmpeg-master-latest-win64-gpl-shared\\bin"
os.environ["PATH"] = f"{FFMPEG_PATH};{os.environ['PATH']}"
print(f"设置ffmpeg路径: {FFMPEG_PATH}")

# 配置信息
OPENROUTER_KEY = "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"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

def download_paraformer_model():
    """下载Paraformer模型"""
    print("开始下载Paraformer模型...")
    
    # 设置模型缓存目录
    model_cache_dir = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic")
    os.makedirs(model_cache_dir, exist_ok=True)
    
    # 下载Paraformer模型
    model_id = "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    
    try:
        model_dir = snapshot_download(
            model_id,
            cache_dir=model_cache_dir,
            revision="master"
        )
        
        # 创建一个简单的符号链接或重命名目录，方便代码引用
        target_dir = os.path.join(model_cache_dir, "paraformer-zh")
        if os.path.exists(target_dir):
            print(f"目标目录已存在: {target_dir}")
        else:
            # 在Windows上，我们使用复制而不是符号链接
            import shutil
            shutil.copytree(model_dir, target_dir)
            print(f"模型已复制到: {target_dir}")
        
        print("Paraformer模型下载完成!")
        return target_dir
    except Exception as e:
        print(f"下载模型失败: {e}")
        return None


def download_audio(bvid):
    """下载B站视频的音频"""
    print(f"正在下载BV号为 {bvid} 的视频音频...")
    
    try:
        # 获取视频信息
        v = video.Video(bvid=bvid)
        info = sync(v.get_info())
        title = info['title']
        print(f"获取视频信息成功，标题: {title}")
        
        # 确保中间文件夹存在
        os.makedirs("intermediate_files", exist_ok=True)
        
        # 使用yt-dlp下载音频，添加详细日志
        audio_path = os.path.join("intermediate_files", f"{bvid}.mp3")
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "-o", audio_path,
            f"https://www.bilibili.com/video/{bvid}"
        ]
        
        print(f"执行命令: {' '.join(cmd)}")
        # 直接运行命令，不捕获输出，让输出直接显示在终端上
        result = subprocess.run(cmd)
        print(f"命令返回码: {result.returncode}")
        
        if result.returncode != 0:
            print(f"音频下载失败，返回码: {result.returncode}")
            sys.exit(1)
        
        # 检查文件是否存在
        if os.path.exists(audio_path):
            print(f"音频下载成功: {audio_path}")
            return title, audio_path
        else:
            print(f"音频文件不存在: {audio_path}")
            sys.exit(1)
    except Exception as e:
        print(f"下载音频时发生异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)



def transcribe_audio(audio_path):
    """使用Paraformer模型将音频转换为文字"""
    print(f"正在将音频 {audio_path} 转换为文字...")
    
    try:
        print("正在加载Paraformer模型...")
        # 尝试使用GPU加速，如果不可用则回退到CPU
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            print(f"使用设备: {device}")
        except ImportError:
            device = "cpu"
            print("PyTorch未安装，使用CPU")
        
        # 导入Paraformer相关模块
        from funasr import AutoModel
        
        # 设置本地模型路径
        local_model_path = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic", "paraformer-zh")
        print(f"使用本地模型路径: {local_model_path}")
        
        # 检查模型是否存在，如果不存在则下载
        if not os.path.exists(local_model_path):
            print("模型不存在，开始下载...")
            download_paraformer_model()
        
        # 加载Paraformer模型
        model = AutoModel(
            model=local_model_path,  # 使用本地路径
            model_revision="master",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            trust_remote_code=True,
            device=device,
        )
        print("Paraformer模型加载成功")
        
        # 获取音频时长用于进度显示
        try:
            cmd = [
                "G:\\ffmpeg-master-latest-win64-gpl-shared\\bin\\ffprobe.exe",
                "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            duration = float(result.stdout.strip())
            print(f"音频总时长: {duration:.2f} 秒 ({duration/60:.2f} 分钟)")
        except:
            print("无法获取音频时长，继续转写...")
            duration = None
        
        # 使用Paraformer进行转写，启用VAD断句
        print("正在转写音频，请稍候...")
        res = model.generate(
            input=audio_path,
            cache={},
            language="zh",  # Paraformer主要支持中文
            use_itn=True,
            batch_size_s=60,
            merge_vad=True,  # 启用VAD合并
            merge_length_s=15,  # VAD片段合并长度
            vad_model="fsmn-vad",  # 使用VAD模型
            vad_kwargs={"max_single_segment_time": 30000}  # VAD参数
        )
        
        # 处理转写结果 - Paraformer的VAD会返回分段结果
        print(f"转写结果类型: {type(res)}")
        if isinstance(res, list) and len(res) > 0:
            print(f"转写结果键: {res[0].keys() if isinstance(res[0], dict) else 'N/A'}")
            
            # 获取文本并去除字间空格
            text = res[0]["text"].replace(" ", "")
            
            # 使用时间戳信息来模拟分段
            if "timestamp" in res[0]:
                timestamps = res[0]["timestamp"]
                print(f"时间戳数量: {len(timestamps)}")
                
                # 根据时间戳分段，每隔一段时间添加逗号
                if len(timestamps) > 5:
                    # 计算每个分段的长度
                    total_chars = len(text)
                    segment_size = max(30, total_chars // 20)  # 至少30个字符一段，最多20段
                    
                    text_parts = []
                    for i in range(0, total_chars, segment_size):
                        segment = text[i:i+segment_size]
                        if segment:
                            segment += "，"  # 添加逗号
                            text_parts.append(segment)
                            print(f"分段: {segment[:30]}...")
                    
                    # 组合所有段落，并将最后一个逗号改为句号
                    text = "".join(text_parts)
                    if text and text.endswith("，"):
                        text = text[:-1] + "。"
                else:
                    # 时间戳太少，直接在末尾添加句号
                    if text and text[-1] not in "。！？；：":
                        text += "。"
            else:
                # 没有时间戳信息，直接在末尾添加句号
                if text and text[-1] not in "。！？；：":
                    text += "。"
        else:
            # 如果结果格式不符合预期，使用原始结果
            print("转写结果格式不符合预期，使用原始结果")
            text = str(res).replace(" ", "")
            # 确保文本以句号结尾
            if text and text[-1] not in "。！？；：":
                text += "。"
        
        print(f"音频转写完成，总长度: {len(text)} 字符")
        print(f"转写结果前100字符: {text[:100]}...")
        print(f"转写结果后100字符: {text[-100:]}...")
        return text
    except Exception as e:
        print(f"音频转文字失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

def summarize_content(title, text):
    """使用deepseek模型总结内容"""
    print("正在调用deepseek模型进行总结...")
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "deepseek/deepseek-v3.2",
        "messages": [
            {
                "role": "system",
                "content": "你是一个专业的视频内容总结助手，请根据提供的视频标题和文字稿，对这个视频进行总结，格式需要使用简单的markdown格式，需要保证清晰易读。"
            },
            {
                "role": "user",
                "content": f"视频标题：{title}\n\n文字稿：{text}"
            }
        ],
        "temperature": 0.7,
        "max_tokens": 1000
    }
    
    try:
        response = requests.post(OPENROUTER_URL, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        summary = result["choices"][0]["message"]["content"]
        
        print("内容总结成功")
        return summary
    except requests.RequestException as e:
        print(f"调用deepseek模型失败: {e}")
        sys.exit(1)

def save_transcription(bvid, title, text):
    """保存转录结果为txt格式"""
    # 清理标题中的非法字符，用于文件名
    import re
    # 移除或替换Windows文件名中不允许的字符
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
    # 限制文件名长度
    safe_title = safe_title[:100] if len(safe_title) > 100 else safe_title
    
    # 确保中间文件夹存在
    os.makedirs("intermediate_files", exist_ok=True)
    
    txt_path = os.path.join("intermediate_files", f"{safe_title}_transcription.txt")
    
    # 构建完整的B站视频链接
    video_url = f"https://www.bilibili.com/video/{bvid}"
    
    content = f"视频标题: {title}\n视频链接: {video_url}\n\n转录内容:\n\n{text}"
    
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"转录结果已保存到: {txt_path}")
    return txt_path

def save_summary(bvid, title, summary):
    """保存总结结果为md格式"""
    # 清理标题中的非法字符，用于文件名
    import re
    # 移除或替换Windows文件名中不允许的字符
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
    # 限制文件名长度
    safe_title = safe_title[:100] if len(safe_title) > 100 else safe_title
    
    # 确保最终输出文件夹存在
    os.makedirs("final_outputs", exist_ok=True)
    
    md_path = os.path.join("final_outputs", f"{safe_title}_summary.md")
    
    # 构建完整的B站视频链接
    video_url = f"https://www.bilibili.com/video/{bvid}"
    
    content = f"# {title}\n\n## 视频链接\n{video_url}\n\n## 内容总结\n{summary}"
    
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"总结结果已保存到: {md_path}")
    return md_path

def main():
    """主函数"""
    print("\n" + "="*50)
    print("B站视频内容总结脚本")
    print("="*50)
    
    try:
        # 检查模型是否存在
        print("\n0. 检查Paraformer模型")
        local_model_path = os.path.join(os.path.dirname(__file__), "model_cache", "models", "iic", "paraformer-zh")
        if not os.path.exists(local_model_path):
            print("   模型不存在，开始下载...")
            download_paraformer_model()
        else:
            print("   模型已存在，跳过下载")
        
        print(f"\n1. 使用预配置的视频链接")
        url = VIDEO_URL
        print(f"处理的URL: {url}")
        
        # 从URL中提取BV号
        print("\n2. 提取BV号")
        if "BV" in url:
            # 找到BV号的起始位置
            bv_start = url.find("BV")
            # 找到问号或斜杠的位置，如果没有则取字符串末尾
            question_mark = url.find("?")
            slash = url.find("/", bv_start + 2)  # 从BV后第2个字符开始找斜杠
            end_pos = -1
            
            # 取最早出现的结束位置
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
            print(f"提取到的BV号: {bvid}")
        else:
            print("无效的B站视频URL，无法提取BV号")
            sys.exit(1)
        
        # 执行完整流程
        print("\n3. 开始执行完整流程")
        
        # 检查是否已有音频文件
        audio_path = os.path.join("intermediate_files", f"{bvid}.mp3")
        if os.path.exists(audio_path):
            print(f"\n   3.1 音频文件已存在，跳过下载: {audio_path}")
            # 直接获取视频标题
            print("   3.2 获取视频标题...")
            v = video.Video(bvid=bvid)
            info = sync(v.get_info())
            title = info['title']
            print(f"   视频标题: {title}")
        else:
            # 1. 下载音频
            print("\n   3.1 下载音频")
            title, audio_path = download_audio(bvid)
        
        # 2. 转写音频为文字
        print("\n   3.3 音频转文字")
        text = transcribe_audio(audio_path)
        
        # 保存转录结果
        print("\n   3.3.1 保存转录结果")
        txt_path = save_transcription(bvid, title, text)
        
        # 3. 调用deepseek模型进行总结
        print("\n   3.4 内容总结")
        summary = summarize_content(title, text)
        
        # 4. 保存总结结果
        print("\n   3.5 保存总结")
        md_path = save_summary(bvid, title, summary)
        
        # 5. 保留音频文件在中间文件夹中
        print("\n   3.6 保留音频文件")
        print(f"   音频文件已保留在: {audio_path}")
        
        print("\n" + "="*50)
        print("任务完成")
        print("="*50)
        print(f"转录结果已保存到: {txt_path}")
        print(f"总结结果已保存到: {md_path}")
    except Exception as e:
        print(f"\n发生异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        print("\n任务失败，请查看以上错误信息")

if __name__ == "__main__":
    main()
