"""
FastAPI 后端 - Bilibili 视频总结服务
"""
import os
import sys
import time
import asyncio
import threading
from typing import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# 配置 ffmpeg 路径
ffmpeg_path = os.path.join(os.getcwd(), "ffmpeg")
if ffmpeg_path not in os.environ["PATH"]:
    os.environ["PATH"] = f"{ffmpeg_path};{os.environ['PATH']}"

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from bili_core import (
    extract_bvid_and_p,
    get_video_info,
    download_audio,
    transcribe_audio,
    summarize_content_stream,
    save_results,
)

# 数据模型
class VideoURL(BaseModel):
    url: str
    p: int = 1

class ProcessResponse(BaseModel):
    status: str
    message: str
    data: dict = None

# 存储处理状态（生产环境应该用 Redis）
process_status = {}

# 全局处理状态
is_processing = False
current_task_id = None

# 心跳检测相关
last_heartbeat = datetime.now()
HEARTBEAT_TIMEOUT = 30  # 30秒没有心跳就自动退出
should_exit = False

# 数据模型
class HeartbeatData(BaseModel):
    timestamp: float = None


def check_heartbeat():
    """检查心跳，如果超时则退出程序"""
    global should_exit
    while True:
        time.sleep(5)  # 每5秒检查一次
        elapsed = (datetime.now() - last_heartbeat).total_seconds()
        if elapsed > HEARTBEAT_TIMEOUT:
            print(f"[心跳检测] {HEARTBEAT_TIMEOUT}秒未收到前端心跳，准备退出...")
            should_exit = True
            # 给主循环一些时间处理
            time.sleep(1)
            os._exit(0)  # 强制退出


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global last_heartbeat
    # 启动时创建目录
    os.makedirs("intermediate_files", exist_ok=True)
    os.makedirs("final_outputs", exist_ok=True)
    
    # 初始化心跳时间
    last_heartbeat = datetime.now()
    
    # 启动心跳检测线程
    heartbeat_thread = threading.Thread(target=check_heartbeat, daemon=True)
    heartbeat_thread.start()
    print(f"[系统] 心跳检测已启动，超时时间: {HEARTBEAT_TIMEOUT}秒")
    
    yield
    # 关闭时清理
    print("[系统] 服务正在关闭...")

app = FastAPI(
    title="Bilibili 视频总结 API",
    description="提供视频信息获取、音频下载、转录、AI总结等功能",
    version="2.0.0",
    lifespan=lifespan
)

# 配置 CORS，允许前端访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """根路径，返回 API 信息"""
    return {
        "name": "Bilibili 视频总结 API",
        "version": "2.0.0",
        "endpoints": {
            "获取视频信息": "POST /api/video-info",
            "处理视频（流式）": "POST /api/process-stream",
            "下载文件": "GET /api/download/{filename}",
        }
    }


@app.post("/api/video-info")
async def get_video_info_api(video: VideoURL):
    """
    获取 Bilibili 视频信息
    """
    try:
        bvid, p = extract_bvid_and_p(video.url)
        if not bvid:
            raise HTTPException(status_code=400, detail="无效的 B 站视频链接")
        
        info = get_video_info(bvid)
        
        return {
            "status": "success",
            "data": {
                "bvid": bvid,
                "p": p,
                "title": info['title'],
                "desc": info['desc'],
                "pic": info['pic'],
                "owner": info['owner'],
                "duration": info['duration'],
                "stat": info['stat'],
                "pages": info.get('pages', [])
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def process_video_stream(url: str, p: int) -> AsyncGenerator[str, None]:
    """
    流式处理视频，生成 SSE 事件
    """
    try:
        timing_stats = {}
        total_start = time.time()
        
        # 1. 提取 BV 号
        bvid, default_p = extract_bvid_and_p(url)
        if not bvid:
            yield f'data: {{"type": "error", "message": "无效的 B 站视频链接"}}\n\n'
            return
        
        p = p or default_p
        
        # 2. 获取视频信息（快速完成）
        yield f'data: {{"type": "progress", "step": 1, "message": "获取视频信息...", "progress": 5}}\n\n'
        info = get_video_info(bvid)
        title = info['title']
        if len(info['pages']) > 1 and p <= len(info['pages']):
            title = f"{title} - {info['pages'][p-1]['part']}"
        
        # 立即标记第一步完成，进入第二步
        yield f'data: {{"type": "progress", "step": 1, "message": "视频信息已获取", "progress": 8}}\n\n'
        
        # 3. 下载音频
        step_start = time.time()
        print("[SSE] 发送: progress step=2 progress=10")
        yield f'data: {{"type": "progress", "step": 2, "message": "正在下载音频...", "progress": 10}}\n\n'
        
        # 在后台运行下载，同时发送进度更新
        download_done = threading.Event()
        download_result = [None]
        download_error = [None]
        
        def run_download():
            try:
                result = download_audio(bvid, p, None)
                download_result[0] = result
            except Exception as e:
                download_error[0] = str(e)
            finally:
                download_done.set()
        
        # 启动下载线程
        download_thread = threading.Thread(target=run_download)
        download_thread.start()
        
        # 等待下载完成，同时发送进度更新
        progress = 10
        while not download_done.is_set():
            await asyncio.sleep(1)  # 每秒更新一次
            progress = min(24, progress + 1)  # 缓慢增加进度到24%
            elapsed = time.time() - step_start
            print(f"[SSE] 发送: progress step=2 progress={progress} elapsed={elapsed:.0f}s")
            yield f'data: {{"type": "progress", "step": 2, "message": "正在下载音频... (已用时 {elapsed:.0f} 秒)", "progress": {progress}}}\n\n'
        
        # 检查是否有错误
        if download_error[0]:
            raise Exception(download_error[0])
        
        title, audio_path = download_result[0]
        timing_stats['音频下载'] = time.time() - step_start
        yield f'data: {{"type": "progress", "step": 2, "message": "音频下载完成", "progress": 25, "timing": {{"音频下载": {timing_stats["音频下载"]:.1f}}}}}\n\n'
        
        # 4. 转录音频
        step_start = time.time()
        yield f'data: {{"type": "progress", "step": 3, "message": "正在转录音频（这可能需要一些时间）...", "progress": 30}}\n\n'
        
        # 在后台运行转录，同时发送进度更新
        import threading
        transcription_done = threading.Event()
        transcription_result = [None]
        transcription_error = [None]
        
        def run_transcription():
            try:
                result = transcribe_audio(audio_path, None)
                transcription_result[0] = result
            except Exception as e:
                transcription_error[0] = str(e)
            finally:
                transcription_done.set()
        
        # 启动转录线程
        transcription_thread = threading.Thread(target=run_transcription)
        transcription_thread.start()
        
        # 等待转录完成，同时发送进度更新
        progress = 30
        while not transcription_done.is_set():
            await asyncio.sleep(2)  # 每2秒更新一次
            progress = min(60, progress + 2)  # 缓慢增加进度到60%
            elapsed = time.time() - step_start
            yield f'data: {{"type": "progress", "step": 3, "message": "正在转录音频... (已用时 {elapsed:.0f} 秒)", "progress": {progress}}}\n\n'
        
        # 检查是否有错误
        if transcription_error[0]:
            raise Exception(transcription_error[0])
        
        text = transcription_result[0]
        timing_stats['音频转录'] = time.time() - step_start
        
        # 清理音频文件
        if os.path.exists(audio_path):
            os.remove(audio_path)
        
        yield f'data: {{"type": "progress", "step": 3, "message": "音频转录完成", "progress": 65, "timing": {{"音频转录": {timing_stats["音频转录"]:.1f}}}}}\n\n'
        yield f'data: {{"type": "transcription", "text_length": {len(text)}}}\n\n'
        
        # 5. AI 总结（流式）
        step_start = time.time()
        yield f'data: {{"type": "progress", "step": 4, "message": "AI 正在总结内容...", "progress": 70}}\n\n'
        
        full_summary = ""
        chunk_count = 0
        for chunk in summarize_content_stream(title, text, None):
            full_summary += chunk
            chunk_count += 1
            # 发送流式总结内容
            print(f"[SSE] 发送: summary_chunk ({len(chunk)} 字符)")
            yield f'data: {{"type": "summary_chunk", "chunk": {repr(chunk)}}}\n\n'
            # 每5个chunk让出一次控制权，确保数据立即发送
            if chunk_count % 5 == 0:
                await asyncio.sleep(0)
            # 每10个chunk发送一次进度更新
            if chunk_count % 10 == 0:
                progress = 70 + min(25, chunk_count / 2)
                print(f"[SSE] 发送: progress step=4 progress={progress} chars={chunk_count}")
                yield f'data: {{"type": "progress", "step": 4, "message": "AI 正在总结内容... ({chunk_count} 字符)", "progress": {progress}}}\n\n'
        
        timing_stats['AI总结'] = time.time() - step_start
        total_time = time.time() - total_start
        timing_stats['总耗时'] = total_time
        
        # 6. 保存结果
        txt_path, md_path = save_results(bvid, title, text, full_summary, p)
        
        # 发送完成事件
        yield f'data: {{"type": "complete", "message": "处理完成！", "timing": {timing_stats}, "files": {{"txt": "{os.path.basename(txt_path)}", "md": "{os.path.basename(md_path)}"}}}}\n\n'
        
    except Exception as e:
        yield f'data: {{"type": "error", "message": "{str(e)}"}}\n\n'


@app.get("/api/status")
async def get_status():
    """
    获取当前处理状态
    """
    global is_processing, current_task_id
    return {
        "is_processing": is_processing,
        "current_task": current_task_id,
        "timestamp": time.time()
    }


@app.post("/api/process-stream")
async def process_stream(video: VideoURL):
    """
    流式处理视频，返回 SSE 事件流
    """
    global is_processing, current_task_id
    
    # 检查是否已有任务在处理
    if is_processing:
        raise HTTPException(
            status_code=423, 
            detail="已有视频正在处理中，请等待当前任务完成"
        )
    
    # 标记为处理中
    is_processing = True
    current_task_id = f"{video.url}_{time.time()}"
    
    try:
        async def stream_with_cleanup():
            global is_processing, current_task_id
            try:
                async for chunk in process_video_stream(video.url, video.p):
                    yield chunk
            finally:
                # 无论成功还是失败，都重置处理状态
                is_processing = False
                current_task_id = None
                print(f"[系统] 处理任务结束，状态已重置")
        
        return StreamingResponse(
            stream_with_cleanup(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Content-Type": "text/event-stream",
            }
        )
    except Exception as e:
        # 发生异常时重置状态
        is_processing = False
        current_task_id = None
        raise e


@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """
    下载生成的文件
    """
    # 安全检查：只允许下载 final_outputs 目录下的文件
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="非法文件名")
    
    file_path = os.path.join("final_outputs", filename)
    if not os.path.exists(file_path):
        # 也检查 intermediate_files
        file_path = os.path.join("intermediate_files", filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    
    return FileResponse(file_path, filename=filename)


@app.get("/api/health")
async def health_check():
    """健康检查接口"""
    return {"status": "healthy", "timestamp": time.time()}


@app.get("/api/test-sse")
async def test_sse():
    """测试 SSE 流是否工作"""
    async def event_generator():
        for i in range(10):
            yield f'data: {{"type": "test", "message": "消息 {i+1}", "progress": {(i+1)*10}}}\n\n'
            await asyncio.sleep(1)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
        }
    )


@app.post("/api/heartbeat")
async def heartbeat(data: HeartbeatData = None):
    """
    接收前端心跳，更新最后心跳时间
    """
    global last_heartbeat
    last_heartbeat = datetime.now()
    return {"status": "ok", "message": "心跳已接收"}


@app.post("/api/shutdown")
async def shutdown():
    """
    接收前端关闭信号，准备退出
    """
    global should_exit
    should_exit = True
    print("[系统] 收到前端关闭信号，准备退出...")
    
    # 在后台线程中延迟退出，给响应时间
    def delayed_exit():
        time.sleep(1)
        os._exit(0)
    
    threading.Thread(target=delayed_exit, daemon=True).start()
    return {"status": "ok", "message": "服务即将关闭"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
