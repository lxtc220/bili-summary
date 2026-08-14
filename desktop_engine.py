"""B站视频总结桌面版后台引擎。

引擎进程不创建窗口，只通过标准输入/输出传递 JSONL 消息。FunASR、Torch、
yt-dlp 和 LLM 调用全部留在这个进程中，避免阻塞或拖垮 PySide6 界面进程。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_log_dir() -> Path:
    preferred = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BiliSummary" / "logs"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = PROJECT_ROOT / "runtime_logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


LOG_DIR = _resolve_log_dir()
LOG_PATH = LOG_DIR / "engine.log"


class _TeeStream:
    """同时写入原始流和引擎日志，便于桌面端定位异常。"""

    def __init__(self, original, log_file):
        self.original = original
        self.log_file = log_file

    def write(self, value):
        self.original.write(value)
        self.log_file.write(value)
        self.log_file.flush()

    def flush(self):
        self.original.flush()
        self.log_file.flush()

    def isatty(self):
        return False


_log_file = LOG_PATH.open("a", encoding="utf-8", buffering=1)
_original_stderr = sys.stderr
sys.stderr = _TeeStream(_original_stderr, _log_file)
_protocol_stdout = sys.stdout
_emit_lock = threading.Lock()


def emit(event: str, **payload: Any) -> None:
    """向 GUI 输出一行 JSON，协议输出始终保持纯 JSONL。"""
    message = {"event": event, **payload}
    with _emit_lock:
        _protocol_stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        _protocol_stdout.flush()


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _run_core(callback: Callable[[], Any]) -> Any:
    """隔离第三方库可能写入 stdout 的日志，避免污染 JSONL 协议。"""
    with redirect_stdout(sys.stderr):
        return callback()


# bili_core 会保证 funasr 先于 bilibili_api / modelscope 加载；同时把其导入时
# 可能产生的第三方输出转到日志，GUI 只接收 JSONL。
with redirect_stdout(sys.stderr):
    import bili_core


_cancel_event = threading.Event()
_busy_lock = threading.Lock()
_busy = False
_task_thread: threading.Thread | None = None


def _progress(message: str) -> None:
    _check_cancelled()
    emit("progress", message=_one_line(message))


def _check_cancelled() -> None:
    if _cancel_event.is_set():
        raise InterruptedError("用户已取消当前任务")


def _safe_video_info(info: dict[str, Any]) -> dict[str, Any]:
    """只把适合展示的基础字段发给 GUI，避免第三方对象无法序列化。"""
    return {
        "title": info.get("title", ""),
        "owner": info.get("owner", ""),
        "duration": info.get("duration", 0),
        "pubdate": info.get("pubdate", 0),
        "page_count": len(info.get("pages") or []),
    }


def _process_video(command: dict[str, Any]) -> None:
    global _busy, _task_thread

    started_at = time.perf_counter()
    url = str(command.get("url") or "").strip()
    enable_thinking = bool(command.get("enable_thinking", False))
    bvid = None
    page = 1
    cache_hit = False

    try:
        emit("stage", name="解析链接", message="正在解析 B 站视频链接...")
        bvid, page = bili_core.extract_bvid_and_p(url)
        if not bvid:
            raise ValueError("无效的 B 站视频链接")
        task_key = f"{bvid}_p{page}"
        emit("task", bvid=bvid, page=page, task_key=task_key)

        _check_cancelled()
        cached_title, cached_text = _run_core(
            lambda: bili_core.load_cached_transcription(bvid, page)
        )

        if cached_title and cached_text:
            cache_hit = True
            title = cached_title
            text = cached_text
            emit("stage", name="读取缓存", message="已找到转录缓存，跳过下载和语音识别。")
        else:
            emit("stage", name="获取信息", message="正在获取视频信息...")
            # 这里仅用于展示元数据；下载函数内部仍会复用同一套 B 站请求和
            # cookies 规则，业务行为与原版保持一致。
            info = _run_core(lambda: bili_core.get_video_info(bvid))
            emit("video_info", info=_safe_video_info(info))

            _check_cancelled()
            emit("stage", name="下载音频", message="正在下载视频音频...")
            download_started = time.perf_counter()
            title, audio_path = _run_core(
                lambda: bili_core.download_audio(bvid, page, _progress)
            )
            download_seconds = time.perf_counter() - download_started
            emit("timing", name="下载", seconds=round(download_seconds, 2))

            _check_cancelled()
            emit("stage", name="语音识别", message="正在进行语音识别...")
            transcribe_started = time.perf_counter()
            text = _run_core(lambda: bili_core.transcribe_audio(audio_path, _progress))
            transcribe_seconds = time.perf_counter() - transcribe_started
            emit("timing", name="转录", seconds=round(transcribe_seconds, 2))
            _run_core(lambda: bili_core.save_transcription(bvid, title, text, page))

        _check_cancelled()
        emit("stage", name="生成总结", message="正在生成 AI 总结...")
        summary_parts: list[str] = []

        def stream_summary() -> None:
            for chunk in bili_core.summarize_content_stream(
                title,
                text,
                _progress,
                enable_thinking=enable_thinking,
            ):
                _check_cancelled()
                if chunk:
                    summary_parts.append(chunk)
                    emit("summary_chunk", content=chunk)

        _run_core(stream_summary)
        summary = "".join(summary_parts)
        _check_cancelled()

        txt_path, md_path = _run_core(
            lambda: bili_core.save_results(bvid, title, text, summary, page)
        )
        emit(
            "result",
            bvid=bvid,
            page=page,
            title=title,
            summary=summary,
            transcription_path=str(Path(txt_path).resolve()),
            summary_path=str(Path(md_path).resolve()),
            cache_hit=cache_hit,
            elapsed=round(time.perf_counter() - started_at, 2),
        )
    except InterruptedError:
        emit("cancelled", message="已取消当前任务。")
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        message = _one_line(exc)
        if not message:
            message = "处理失败，请查看引擎日志。"
        emit("error", message=message, log_path=str(LOG_PATH.resolve()))
    finally:
        with _busy_lock:
            _busy = False
            if threading.current_thread() is _task_thread:
                _task_thread = None


def _is_busy() -> bool:
    with _busy_lock:
        return _busy


def _start_video_task(command: dict[str, Any]) -> None:
    """在独立线程中执行视频任务，让主循环继续接收取消和退出命令。"""
    global _busy, _task_thread

    with _busy_lock:
        if _busy:
            emit("error", message="已有任务正在处理，请等待当前任务结束。")
            return
        _busy = True

    _cancel_event.clear()
    task_thread = threading.Thread(
        target=_process_video,
        args=(command,),
        name="bili-video-task",
        daemon=True,
    )
    _task_thread = task_thread
    try:
        task_thread.start()
    except Exception as exc:
        with _busy_lock:
            _busy = False
            _task_thread = None
        emit("error", message=f"无法启动后台处理线程：{_one_line(exc)}")


def run_engine() -> int:
    emit("status", state="starting", message="正在启动后台引擎...")
    emit("status", state="warming", message="正在预热语音识别模型，首次启动可能需要一些时间...")
    try:
        _run_core(lambda: bili_core.preload_asr_model(_progress))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit(
            "error",
            message=f"语音识别引擎启动失败：{_one_line(exc)}",
            log_path=str(LOG_PATH.resolve()),
            fatal=True,
        )
        return 2

    emit("ready", message="核心组件已就绪，可以开始处理。", log_path=str(LOG_PATH.resolve()))

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            emit("error", message="后台引擎收到无效命令。", log_path=str(LOG_PATH.resolve()))
            continue

        command_name = command.get("command")
        if command_name == "process":
            _start_video_task(command)
        elif command_name == "cancel":
            if _is_busy():
                _cancel_event.set()
                emit("progress", message="正在取消当前任务，请稍候...")
            else:
                emit("progress", message="当前没有正在处理的任务。")
        elif command_name == "shutdown":
            emit("stopping", message="后台引擎正在退出。")
            _cancel_event.set()
            task_thread = _task_thread
            if task_thread and task_thread.is_alive():
                task_thread.join()
            return 0
        else:
            emit("error", message=f"未知的后台命令：{command_name}")

    _cancel_event.set()
    task_thread = _task_thread
    if task_thread and task_thread.is_alive():
        task_thread.join()
    return 0


if __name__ == "__main__":
    if "--engine" not in sys.argv:
        print("请使用 desktop_app.py 启动桌面程序。")
        raise SystemExit(1)
    os.chdir(PROJECT_ROOT)
    try:
        raise SystemExit(run_engine())
    finally:
        sys.stderr = _original_stderr
        _log_file.close()
