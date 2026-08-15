"""B站视频总结工具 - NiceGUI 网页前端。

只 import bili_core，不做业务逻辑。

设计原则（适合 NiceGUI 的做法）：
  - 主题走 NiceGUI 原生体系：ui.colors 把 primary 设为 B 站粉，
    按钮/输入框/复选框的选中态由 Quasar 自动继承品牌色；
  - 左右布局：左侧工具面板（输入/步骤/视频信息），右侧阅读区
    （总结正文），Tailwind 原生类 + 白卡片细边框；
  - 侧栏步骤条为竖向圆点连线（pending/active/done/skip 四态）。

架构：长任务在后台线程执行，写共享 TaskState（加锁）；页面用 async
ui.timer 渲染快照（timer 回调必须 async，同步回调会阻断事件分发）。
"""

import base64
import datetime
import html as html_lib
import os
import re as re_lib
import sys
import threading
import time

from dotenv import load_dotenv
from nicegui import app, ui
from starlette.responses import HTMLResponse

# 读取 .env（bili_core 内部也会读；这里提前读是为了密钥提示
# 不依赖 bili_core 加载完成）
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_dotenv_path = os.path.join(_PROJECT_ROOT, '.env')
if os.path.exists(_dotenv_path):
    load_dotenv(_dotenv_path)

DEFAULT_PORT = 8080

BILI_PINK = "#fb7299"
BILI_BLUE = "#00aeec"


# ---------------------------------------------------------------------------
# bili_core 懒加载（首屏渲染不等待 funasr/torch 等重型依赖）
# ---------------------------------------------------------------------------

_core = None
_backend_lock = threading.Lock()
_asr_preload_started = False
_asr_preload_lock = threading.Lock()


def _get_core():
    """首次调用时 import bili_core 并缓存（线程安全）。"""
    global _core
    if _core is None:
        with _backend_lock:
            if _core is None:
                import bili_core
                _core = bili_core
    return _core


def _start_asr_preload():
    """进程内只启动一次的后台预热线程：先 import bili_core，再预加载 ASR。

    模型加载成功与否不影响处理流程——若加载未完成，处理流程到转录步骤时
    transcribe_audio 内部会阻塞等待（bili_core 的双检锁保证只加载一次）。
    """
    global _asr_preload_started
    with _asr_preload_lock:
        if _asr_preload_started:
            return
        _asr_preload_started = True

    def worker():
        try:
            _get_core()
        except Exception as e:
            print(f"bili_core 导入失败: {e}", file=sys.stderr)
        try:
            _get_core().preload_asr_model()
        except Exception as e:
            print(f"ASR 预加载失败（首次转写时会自动重试）: {e}", file=sys.stderr)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# 自动退出：30 分钟无浏览器连接则退出进程（start.bat 双击拉起的场景）
# ---------------------------------------------------------------------------

_conn_lock = threading.Lock()
_conn_count = 0
_last_active = time.time()


def _on_connect():
    global _conn_count, _last_active
    with _conn_lock:
        _conn_count += 1
        _last_active = time.time()
    print(f"[UI] 浏览器连接 (当前 {_conn_count})", flush=True)


def _on_disconnect():
    global _conn_count
    with _conn_lock:
        _conn_count = max(0, _conn_count - 1)
    print(f"[UI] 浏览器断开 (当前 {_conn_count})", flush=True)


app.on_connect(_on_connect)
app.on_disconnect(_on_disconnect)


def _idle_monitor():
    time.sleep(60)  # 启动宽限期
    while True:
        with _conn_lock:
            active = _conn_count > 0
            idle_for = time.time() - _last_active
        if not active and idle_for >= 30 * 60:
            print("检测到长时间无连接，正在自动退出后台进程...")
            os._exit(0)
        time.sleep(30)


threading.Thread(target=_idle_monitor, daemon=True).start()


# ---------------------------------------------------------------------------
# 打印页：A4 排版的总结（浏览器打印方案，LLM 输出先消毒再转 HTML）
# ---------------------------------------------------------------------------

_print_pages: dict[str, str] = {}
_print_lock = threading.Lock()


def _sanitize_markdown_html(summary_md: str) -> str:
    """markdown → HTML，并剥离 LLM 输出里可能携带的活 HTML 与事件属性。"""
    import markdown as md_lib

    body_html = md_lib.markdown(
        summary_md,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    body_html = re_lib.sub(
        r"<(script|iframe|style|object|embed|link|meta)\b[^>]*>.*?</\1>"
        r"|<(script|iframe|style|object|embed|link|meta)\b[^>]*/?>",
        "",
        body_html,
        flags=re_lib.IGNORECASE | re_lib.DOTALL,
    )
    body_html = re_lib.sub(
        r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        body_html,
        flags=re_lib.IGNORECASE,
    )
    return re_lib.sub(r"javascript:", "", body_html, flags=re_lib.IGNORECASE)


def _build_print_html(title: str, summary_md: str, url: str) -> str:
    """把总结 markdown 组装成完整的 A4 打印页 HTML。"""
    body_html = _sanitize_markdown_html(summary_md)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    title_esc = html_lib.escape(title)
    url_esc = html_lib.escape(url)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>视频总结 - {title_esc}</title>
<style>
    @page {{ size: A4; margin: 18mm 16mm; }}
    body {{
        font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
        font-size: 11pt; line-height: 1.75; color: #1f2937; margin: 0;
    }}
    h1 {{ font-size: 20pt; color: #111827; margin: 0 0 4mm 0; page-break-after: avoid; }}
    h2 {{ font-size: 15pt; color: #111827; margin: 7mm 0 3mm 0; page-break-after: avoid; }}
    h3 {{ font-size: 12.5pt; color: #1f2937; margin: 5mm 0 2.5mm 0; page-break-after: avoid; }}
    p {{ margin: 0 0 3mm 0; }}
    ul, ol {{ margin: 0 0 3mm 0; padding-left: 6mm; }}
    li {{ margin-bottom: 1mm; }}
    strong {{ color: #111827; }}
    blockquote {{ margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #e5e7eb; color: #4b5563; }}
    code {{ font-family: Consolas, "Courier New", monospace; font-size: 10pt; background: #f3f4f6; padding: 0.5mm 1.5mm; border-radius: 2px; }}
    pre {{ background: #f9fafb; border: 1px solid #e5e7eb; padding: 3mm 4mm; border-radius: 4px; page-break-inside: avoid; }}
    pre code {{ background: none; padding: 0; }}
    table {{ border-collapse: collapse; width: 100%; margin: 3mm 0; page-break-inside: avoid; }}
    th, td {{ border: 1px solid #d1d5db; padding: 1.5mm 3mm; font-size: 10pt; text-align: left; }}
    th {{ background: #f3f4f6; }}
    a {{ color: #2563eb; text-decoration: none; word-break: break-all; }}
    hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 5mm 0; }}
    .meta {{ color: #6b7280; font-size: 10pt; margin-bottom: 6mm; }}
    .footer {{ margin-top: 8mm; padding-top: 3mm; border-top: 1px solid #e5e7eb; color: #9ca3af; font-size: 9pt; }}
    @media print {{ .footer {{ position: fixed; bottom: 0; width: 100%; }} }}
</style>
</head>
<body>
<h1>{title_esc}</h1>
<div class="meta">
    视频链接：<a href="{url_esc}">{url_esc}</a><br>
    生成时间：{now} ｜ B站视频总结工具
</div>
{body_html}
<div class="footer">由 B站视频总结工具 (Bili-summary) 自动生成</div>
<script>window.print();</script>
</body>
</html>"""


@app.get("/print")
def _print_page(key: str) -> HTMLResponse:
    with _print_lock:
        page_html = _print_pages.get(key)
    if page_html is None:
        return HTMLResponse("<h3>打印内容不存在或已过期，请回到主页重新生成。</h3>")
    return HTMLResponse(page_html)


# 完成提示音：Web Audio 合成"叮咚"，无需外部音频文件
_SOUND_JS = """
(function () {
    try {
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        function tone(freq, start, dur, gainPeak) {
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0.0001, ctx.currentTime + start);
            gain.gain.exponentialRampToValueAtTime(gainPeak, ctx.currentTime + start + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + dur);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(ctx.currentTime + start);
            osc.stop(ctx.currentTime + start + dur + 0.05);
        }
        tone(880, 0, 0.25, 0.25);
        tone(1320, 0.18, 0.45, 0.25);
    } catch (e) { console.warn('播放提示音失败:', e); }
})();
"""


# ---------------------------------------------------------------------------
# 页面样式：极少量组件级自定义，整体交给 Tailwind + Quasar 主题
# ---------------------------------------------------------------------------

_CUSTOM_CSS = """
<style>
    /* 与旧版一致：Inter（Latin）+ 系统中文字体回落 */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    html, body, .q-field, .q-btn, .q-checkbox, .q-markdown {
        font-family: 'Inter', system-ui, -apple-system, 'Segoe UI',
                     'Microsoft YaHei', 'PingFang SC', sans-serif !important;
    }
    body { background: #f6f7f9; }
    /* 标题沿用旧版视觉：B 站粉→蓝渐变字，加粗 */
    .bili-title {
        font-size: 1.5rem; font-weight: 800; letter-spacing: -0.3px;
        background: linear-gradient(135deg, #fb7299 0%, #00aeec 100%);
        -webkit-background-clip: text; background-clip: text;
        -webkit-text-fill-color: transparent; color: transparent;
    }
    .bili-card {
        background: #ffffff;
        border: 1px solid #e8eaee;
        border-radius: 14px;
    }
    /* 总结正文阅读体验：本机思源宋体正文 + 思源黑体标题，雅黑兜底 */
    .bili-card.summary-body {
        font-size: 16px; line-height: 1.85;
        font-family: 'Noto Serif SC', 'Microsoft YaHei', serif !important;
    }
    .summary-body h1, .summary-body h2, .summary-body h3, .summary-body h4 {
        font-family: 'Noto Sans SC', 'Microsoft YaHei', sans-serif !important;
        font-weight: 600;
    }
    }
    .side-card {
        background: #ffffff;
        border: 1px solid #e8eaee;
        border-radius: 14px;
        padding: 0.9rem 1rem;
    }
    .step-dot {
        width: 2rem; height: 2rem; border-radius: 9999px;
        display: flex; align-items: center; justify-content: center;
        font-size: 1rem; flex: none; transition: all .25s ease;
    }
    .step-dot-pending { background: #f1f5f9; border: 1px solid #e2e8f0; }
    .step-dot-active  { background: #fb7299; color: #fff; box-shadow: 0 0 0 5px rgba(251,114,153,.14); }
    .step-dot-done    { background: #10b981; color: #fff; }
    .step-dot-skip    { background: #fff; border: 1px dashed #cbd5e1; opacity: .75; }
    .vstep-line { width: 2px; height: 14px; background: #e5e7eb; margin-left: 15px; border-radius: 2px; }
    .vstep-line-done { background: #10b981; }
    .phase-chip {
        display: inline-flex; align-items: center; gap: .3rem;
        font-size: .78rem; font-weight: 600; padding: .18rem .7rem;
        border-radius: 9999px;
    }
    .phase-idle    { background: #f1f5f9; color: #64748b; }
    .phase-running { background: rgba(251,114,153,.1); color: #d6336c; }
    .phase-done    { background: rgba(16,185,129,.12); color: #059669; }
    .phase-error   { background: #fef2f2; color: #dc2626; }
    .timing-row {
        display: flex; justify-content: space-between;
        font-size: .85rem; color: #64748b; padding: .3rem 0;
        border-bottom: 1px dashed #eef0f3;
    }
    .timing-row-total { border-bottom: none; font-weight: 700; color: #1e293b; }
</style>
"""


# ---------------------------------------------------------------------------
# 任务状态与后台流水线
# ---------------------------------------------------------------------------

STEPS = [
    ("📄", "获取信息"),
    ("⬇️", "下载音频"),
    ("🎙️", "音频转录"),
    ("✨", "AI 总结"),
]

MAX_MODEL_WAIT_SEC = 900  # 模型加载最长等待 15 分钟（首次含模型下载）


class TaskCancelled(Exception):
    """用户点击了取消。"""


class TaskState:
    """单个浏览器页签的任务状态，后台线程写、页面 timer 读，访问加锁。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.reset()

    def reset(self):
        self.phase = "idle"          # idle / running / done / error / cancelled
        self.step = 0                # 1-4 对应 STEPS；5 表示已完成
        self.message = ""            # 当前阶段的具体提示
        self.url = ""
        self.bvid = None
        self.p = 1
        self.task_key = ""
        self.title = ""
        self.cache_hit = False
        self.video_info = None       # get_video_info 返回的 dict
        self.cover_src = None        # base64 data URI 或原始 URL
        self.stream_text = ""        # 流式输出中的总结
        self.final_summary = ""
        self.timing = None
        self.error = ""
        self.sound_pending = False   # 完成后由页面一次性播放提示音
        self.download_time = 0.0
        self.transcribe_time = 0.0
        self.started_at = 0.0

    def update(self, **kwargs):
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)


def _fetch_cover_base64(pic_url: str) -> str:
    """下载封面并转 base64 内嵌，绕开 B 站防盗链。失败时退回原 URL。"""
    try:
        import requests

        if pic_url.startswith("//"):
            pic_url = "https:" + pic_url
        response = requests.get(
            pic_url,
            headers={
                "Referer": "https://www.bilibili.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.10; Win64; x64) AppleWebKit/537.36",
            },
            timeout=10,
        )
        if response.status_code == 200:
            b64 = base64.b64encode(response.content).decode("utf-8")
            mime = "image/png" if pic_url.lower().endswith(".png") else "image/jpeg"
            return f"data:{mime};base64,{b64}"
    except Exception:
        pass
    return pic_url


def _run_pipeline(state: TaskState, url: str, enable_thinking: bool):
    """后台流水线线程：解析 → (缓存判断) → 信息 → 下载 → 转录 → 流式总结 → 落盘。

    所有用户可见的错误以中文写入 state.error，由页面展示。
    """
    try:
        state.update(phase="running", step=1, message="", error="",
                     started_at=time.time())

        def progress(msg):
            # 取消只在回调检查点生效（与桌面版引擎行为一致）
            if state.cancel_event.is_set():
                raise TaskCancelled()
            state.update(message=msg)

        core = _get_core()
        bvid, p = core.extract_bvid_and_p(url)
        if not bvid:
            state.update(phase="error", error="无效的 B 站视频链接")
            return
        state.update(bvid=bvid, p=p, task_key=f"{bvid}_p{p}")

        # 磁盘缓存命中：转录稿已落盘，跳过前三步直接进入 AI 总结
        cached_title, cached_text = core.load_cached_transcription(bvid, p)
        if cached_text:
            state.update(cache_hit=True, step=4,
                         title=cached_title or bvid,
                         stream_text="", message="已命中转录缓存，正在重新生成总结…")
        else:
            # 第 1 步：获取视频信息（含封面与分 P 标题）
            state.update(step=1, message="正在获取视频信息...")
            info = core.get_video_info(bvid)
            title = info["title"]
            if len(info.get("pages", [])) > 1 and 1 <= p <= len(info["pages"]):
                title = f"{title} - {info['pages'][p - 1]['part']}"
            state.update(video_info=info, title=title)
            # 封面在后台线程取（requests 阻塞调用不能放进 UI 定时器）
            state.update(cover_src=_fetch_cover_base64(info.get("pic", "")))

            # 第 2 步：下载音频
            state.update(step=2, message="正在下载音频...")
            started = time.time()
            title, audio_path = core.download_audio(bvid, p, progress)
            state.update(title=title, download_time=time.time() - started)

            # 第 3 步：等待模型（预热未完成时）并转录
            state.update(step=3, message="正在准备语音识别...")
            if core.get_asr_model_status() == "loading":
                wait_start = time.time()
                while core.get_asr_model_status() == "loading":
                    if state.cancel_event.is_set():
                        raise TaskCancelled()
                    elapsed = int(time.time() - wait_start)
                    if elapsed >= MAX_MODEL_WAIT_SEC:
                        raise Exception(
                            f"语音识别引擎加载超时（已等待超过 {MAX_MODEL_WAIT_SEC // 60} 分钟），"
                            "请检查后台日志或重启服务"
                        )
                    state.update(message=f"语音识别引擎加载中… 已等待 {elapsed} 秒")
                    time.sleep(1)

            started = time.time()
            text = core.transcribe_audio(audio_path, progress)
            state.update(transcribe_time=time.time() - started)

            if os.path.exists(audio_path):
                os.remove(audio_path)

            # 转写完成即落盘：即使后续 AI 总结失败，下次同 BV 也能跳过前三步
            core.save_transcription(bvid, state.title, text, p)

            # 第 4 步：流式 AI 总结
            state.update(step=4, stream_text="", message="正在调用 AI 模型…")

        full_summary = ""
        summarize_started = time.time()
        for chunk in core.summarize_content_stream(
            state.title,
            cached_text if state.cache_hit else text,
            progress,
            enable_thinking=enable_thinking,
        ):
            full_summary += chunk
            state.update(stream_text=full_summary)

        summarize_time = time.time() - summarize_started
        core.save_results(bvid, state.title, cached_text if state.cache_hit else text, full_summary, p)
        timing = {
            "音频下载": state.download_time,
            "音频转录": state.transcribe_time,
            "AI 总结": summarize_time,
            "总耗时": state.download_time + state.transcribe_time + summarize_time,
        }
        state.update(
            phase="done", step=5, final_summary=full_summary, timing=timing,
            # 完成后清掉最后一条进度提示，改为展示总耗时
            message=f"总耗时 {timing['总耗时']:.1f} 秒",
            sound_pending=True,
        )
    except TaskCancelled:
        state.update(phase="cancelled", message="已取消当前任务。")
    except Exception as exc:
        state.update(phase="error", error=f"处理失败: {exc}")
        import traceback
        traceback.print_exc(file=sys.stderr)


# ---------------------------------------------------------------------------
# 页面（左右布局：左侧工具面板 + 右侧阅读区）
# ---------------------------------------------------------------------------

@ui.page("/")
def main_page():
    state = TaskState()
    _start_asr_preload()

    # 品牌色注入 Quasar 主题：按钮/复选框/输入框焦点色自动继承
    ui.colors(primary=BILI_PINK, secondary=BILI_BLUE)
    ui.add_head_html(_CUSTOM_CSS)

    # ---- 左侧工具面板 ----
    with ui.left_drawer(top_corner=True, bottom_corner=True) \
            .classes("w-[340px] px-4 py-5 gap-3"):
        with ui.column().classes("w-full gap-1 px-1 pb-1"):
            ui.label("🎬 B站视频总结").classes("bili-title")
            ui.label("粘贴链接，自动转录并总结").classes("text-xs text-slate-400")

        # 输入与操作
        with ui.column().classes("w-full side-card gap-2"):
            url_input = (ui.input(placeholder="https://www.bilibili.com/video/BV...")
                         .props("dense outlined clearable")
                         .classes("w-full")
                         .on("keydown.enter", lambda: on_start()))
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                start_btn = (ui.button("开始处理", on_click=lambda: on_start())
                             .props("unelevated no-caps")
                             .classes("flex-1 no-shadow"))
                cancel_btn = (ui.button("取消", on_click=lambda: on_cancel())
                              .props("outline no-caps color=grey-7")
                              .classes("px-4 no-shadow"))
                cancel_btn.set_visibility(False)
            thinking_check = ui.checkbox("🧠 深度思考（更慢但更详细）", value=True) \
                .classes("text-xs text-slate-500")
            asr_badge = ui.label().classes("text-xs text-slate-400")

        if not os.getenv("LLM_API_KEY"):
            ui.label("⚠️ 未配置 LLM_API_KEY，AI 总结不可用，请在 .env 中配置。") \
                .classes("text-xs text-amber-600 px-1")

        # 处理进度
        with ui.column().classes("w-full side-card gap-2"):
            with ui.row().classes("w-full items-center justify-between"):
                phase_chip = ui.label("等待开始").classes("phase-chip phase-idle")
                phase_msg = ui.label().classes(
                    "text-xs text-slate-400 max-w-[58%] truncate")
            step_dots, step_labels, step_lines = [], [], []
            for i, (icon, name) in enumerate(STEPS):
                if i > 0:
                    line = ui.element("div").classes("vstep-line")
                    step_lines.append(line)
                with ui.row().classes("items-center gap-3 no-wrap"):
                    dot = ui.label(icon).classes("step-dot step-dot-pending")
                    lbl = ui.label(name).classes("text-sm text-slate-400")
                    step_dots.append(dot)
                    step_labels.append(lbl)
            timing_container = ui.column().classes("w-full pt-1")
            timing_container.set_visibility(False)

        # 视频信息
        with ui.column().classes("w-full side-card gap-2") as info_card:
            cover_img = ui.image().classes("w-full rounded-lg")
            info_title = ui.label().classes(
                "text-sm font-semibold text-slate-800 leading-snug")
            with ui.row().classes("items-center gap-3 text-xs text-slate-500"):
                info_owner = ui.label()
                info_meta = ui.label()
        info_card.set_visibility(False)

    # ---- 右侧阅读区 ----
    with ui.column().classes("w-full max-w-5xl mx-auto px-8 py-8 gap-4"):
        with ui.row().classes("w-full items-center justify-between px-1"):
            ui.label("视频总结").classes("text-lg font-semibold text-slate-700")
            print_btn = (ui.button("🖨️ 打印总结", on_click=lambda: on_print())
                         .props("outline no-caps flat color=grey-7")
                         .classes("text-xs no-shadow"))
            print_btn.set_visibility(False)
        with ui.card().classes("bili-card summary-body w-full p-6 min-h-[300px] no-shadow"):
            summary_view = ui.markdown(
                "💡 在左侧输入 B 站视频链接，点击「开始处理」生成总结。"
            ).classes("w-full")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def on_start():
        url = (url_input.value or "").strip()
        print(f"[UI] 开始处理: {url!r}", flush=True)
        if not url:
            ui.notify("请先粘贴 B 站视频链接。", type="warning")
            return
        if state.phase == "running":
            ui.notify("已有任务正在处理，请等待完成或先取消。", type="warning")
            return
        with state.lock:
            state.reset()
            state.url = url
        state.cancel_event = threading.Event()
        threading.Thread(
            target=_run_pipeline,
            args=(state, url, thinking_check.value),
            daemon=True,
        ).start()

    def on_cancel():
        if state.phase == "running":
            state.cancel_event.set()
            ui.notify("正在取消当前任务，请稍候…")
        else:
            ui.notify("当前没有正在处理的任务。")

    def on_print():
        with state.lock:
            title, summary, url, key_src = (
                state.title, state.final_summary, state.url, state.task_key)
        if not summary:
            ui.notify("尚无总结内容可打印。", type="warning")
            return
        key = f"{key_src}_{int(time.time())}"
        with _print_lock:
            _print_pages[key] = _build_print_html(title or "视频总结", summary, url)
            # 只保留最近 20 份打印页，避免长驻进程内存增长
            for old_key in list(_print_pages)[:-20]:
                _print_pages.pop(old_key, None)
        ui.open(f"/print?key={key}", new_tab=True)

    # ------------------------------------------------------------------
    # 周期渲染：从 TaskState 快照刷新 UI（内容变化时才更新元素）
    # ------------------------------------------------------------------
    rendered = {"signature": None, "dot_cls": [None] * 4,
                "line_done": [None] * 3, "phase": None, "running": None,
                "asr": None, "cover": None}

    async def refresh():
        with state.lock:
            snap = {
                "phase": state.phase, "step": state.step, "message": state.message,
                "video_info": state.video_info, "cover_src": state.cover_src,
                "timing": state.timing, "error": state.error,
                "cache_hit": state.cache_hit, "title": state.title,
                "stream_text": state.stream_text, "final_summary": state.final_summary,
                "sound_pending": state.sound_pending,
            }

        if snap["sound_pending"]:
            state.update(sound_pending=False)
            ui.run_javascript(_SOUND_JS)

        signature = (
            snap["phase"], snap["step"], snap["message"], snap["cover_src"],
            snap["timing"], snap["error"], snap["cache_hit"],
            len(snap["stream_text"] or ""),
            (snap["stream_text"] or "")[-32:],
            snap["final_summary"] is not None,
        )
        streaming = snap["phase"] == "running" and snap["step"] == 4
        if signature != rendered["signature"] or streaming:
            rendered["signature"] = signature
            _render_dynamic(snap)
        _render_controls(snap)

    def _render_dynamic(snap):
        # 阶段徽标 + 消息
        phase_map = {
            "idle": ("等待开始", "phase-idle"),
            "running": ("处理中", "phase-running"),
            "done": ("已完成", "phase-done"),
            "cancelled": ("已取消", "phase-idle"),
            "error": ("出错", "phase-error"),
        }
        chip_text, chip_cls = phase_map.get(snap["phase"], ("等待开始", "phase-idle"))
        if rendered["phase"] != snap["phase"]:
            phase_chip.text = chip_text
            phase_chip.classes(
                add=chip_cls,
                remove=" ".join(c for c in
                                ("phase-idle", "phase-running", "phase-done", "phase-error")
                                if c != chip_cls))
            rendered["phase"] = snap["phase"]
        msg = (snap["error"] or snap["message"] or "").strip()
        if phase_msg.text != msg:
            phase_msg.text = msg

        # 步骤圆点与竖向连线
        for i in range(len(STEPS)):
            step_num = i + 1
            if snap["phase"] == "done":
                cls = "step-dot-done"
            elif snap["cache_hit"] and step_num <= 3:
                cls = "step-dot-skip"
            elif snap["step"] > step_num:
                cls = "step-dot-done"
            elif snap["step"] == step_num and snap["phase"] == "running":
                cls = "step-dot-active"
            else:
                cls = "step-dot-pending"
            if rendered["dot_cls"][i] != cls:
                step_dots[i].classes(
                    add=cls,
                    remove=" ".join(
                        c for c in ("step-dot-pending", "step-dot-active",
                                    "step-dot-done", "step-dot-skip")
                        if c != cls))
                rendered["dot_cls"][i] = cls
                step_labels[i].classes(
                    add="text-slate-700 font-medium" if cls == "step-dot-active"
                    else "text-slate-400",
                    remove="text-slate-700 font-medium" if cls != "step-dot-active"
                    else "text-slate-400")
            if i < len(step_lines):
                line_done = snap["phase"] == "done" or snap["step"] > i + 1
                if rendered["line_done"][i] != line_done:
                    if line_done:
                        step_lines[i].classes(add="vstep-line-done")
                    else:
                        step_lines[i].classes(remove="vstep-line-done")
                    rendered["line_done"][i] = line_done

        # 耗时统计
        if snap["phase"] == "done" and snap["timing"]:
            timing_container.clear()
            with timing_container:
                ui.separator().classes("mb-1")
                for k, v in snap["timing"].items():
                    is_total = k == "总耗时"
                    ui.label(f"{k}　{v:.1f} 秒").classes(
                        "timing-row timing-row-total" if is_total else "timing-row")
            timing_container.set_visibility(True)
        else:
            timing_container.set_visibility(False)

        # 视频信息卡片
        if snap["video_info"] is not None:
            info = snap["video_info"]
            duration = info.get("duration") or 0
            minutes, seconds = divmod(int(duration), 60)
            cover = snap["cover_src"] or info.get("pic", "")
            if rendered["cover"] != cover:
                cover_img.set_source(cover)
                rendered["cover"] = cover
            info_title.text = snap.get("title") or info.get("title", "")
            info_owner.text = f"UP主 · {info.get('owner', '')}"
            info_meta.text = f"时长 {minutes}:{seconds:02d}"
            info_card.set_visibility(True)

        # 总结正文
        if snap["phase"] == "running" and snap["step"] < 4:
            hint = {
                1: "📄 正在获取视频信息…",
                2: "⬇️ 正在下载音频…",
                3: snap["message"] or "🎙️ 正在语音转文字…",
            }.get(snap["step"], "⏳ 正在处理…")
            summary_view.set_content(hint)
        elif snap["phase"] == "running" and snap["step"] == 4:
            summary_view.set_content(
                snap["stream_text"] + " ▌" if snap["stream_text"]
                else "✨ 正在生成总结…")
        elif snap["phase"] == "done":
            summary_view.set_content(snap["final_summary"])
        elif snap["phase"] == "cancelled":
            summary_view.set_content(snap["message"] or "已取消。")
        elif snap["phase"] == "error":
            summary_view.set_content(f"❌ {snap['error']}")
        elif not snap["stream_text"]:
            summary_view.set_content(
                "💡 在左侧输入 B 站视频链接，点击「开始处理」生成总结。")

        print_btn.set_visibility(snap["phase"] == "done")

    def _render_controls(snap):
        running = snap["phase"] == "running"
        if rendered["running"] != running:
            rendered["running"] = running
            start_btn.disable() if running else start_btn.enable()
            url_input.disable() if running else url_input.enable()
            cancel_btn.set_visibility(running)
        if _core is None:
            asr_text = "⏳ 后端初始化中…"
        else:
            status = _core.get_asr_model_status()
            if status == "loading":
                asr_text = "⏳ 识别引擎加载中（不阻塞下载）"
            elif status == "ready":
                asr_text = "✅ 识别引擎已就绪"
            else:
                asr_text = "🔓 识别引擎待命"
        if rendered["asr"] != asr_text:
            asr_badge.text = asr_text
            rendered["asr"] = asr_text

    # 注意：timer 回调必须是 async。实测同步回调会持续干扰 NiceGUI 的
    # 事件分发（按钮点击/回车均无法到达服务器，页面渲染推送却正常），
    # 这是本次迁移排查半天才定位到的坑，不要改回同步函数。
    async def safe_refresh():
        try:
            await refresh()
        except Exception as exc:
            print(f"[UI] 渲染刷新异常: {exc}", flush=True)

    ui.timer(0.25, safe_refresh)


if __name__ in {"__main__", "__mp_main__"}:
    headless = os.environ.get("BILI_UI_HEADLESS") == "1"
    port = int(os.environ.get("BILI_UI_PORT", str(DEFAULT_PORT)))
    # reload=False：生产入口，避免文件监视器带来的双进程与重载副作用
    ui.run(
        title="B站视频总结工具",
        port=port,
        reload=False,
        show=not headless,
        favicon="🎬",
    )
