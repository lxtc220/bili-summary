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
import json
import os
import re as re_lib
import sys
import threading
import time

from asr_worker import get_asr_worker
from dotenv import load_dotenv
from nicegui import app, run, ui
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from urllib.parse import quote

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
# bili_core 懒加载（funasr/torch 已隔离进 asr_worker 子进程，本进程
# 导入 bili_core 只剩轻依赖，秒级完成；机制保留作保护）
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
    """进程内只启动一次：拉起 ASR 工作子进程并预加载模型。

    主进程从此不再 import funasr/torch——模型加载全部发生在 asr_worker
    子进程里。加载成功与否不影响处理流程：流水线到转录步骤时会等待
    worker 就绪；worker 意外退出会在下次转录时自动重启自愈。
    """
    global _asr_preload_started
    with _asr_preload_lock:
        if _asr_preload_started:
            return
        _asr_preload_started = True

    def boot():
        try:
            get_asr_worker().preload()
        except Exception as e:
            print(f"ASR 工作进程启动失败（首次转写时会自动重试）: {e}", file=sys.stderr)

    threading.Thread(target=boot, daemon=True).start()


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
        if not active and idle_for >= 10 * 60:
            print("检测到长时间无连接，正在自动退出后台进程...")
            os._exit(0)
        time.sleep(30)


threading.Thread(target=_idle_monitor, daemon=True).start()


# ---------------------------------------------------------------------------
# 打印页：A4 排版的总结（浏览器打印方案，LLM 输出先消毒再转 HTML）
# ---------------------------------------------------------------------------

_print_pages: dict[str, str] = {}
# 导出图片的素材 {key: (标题, 总结 markdown)}，与打印页同一时机注册
_export_sources: dict[str, tuple] = {}
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


def _build_print_html(title: str, summary_md: str) -> str:
    """把总结 markdown 组装成完整的 A4 打印页 HTML。

    summary_md 为统一版式（标题开头、正文、视频链接收尾），打印页
    不再自带头部，只在页脚补充生成时间。
    """
    body_html = _sanitize_markdown_html(summary_md)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    title_esc = html_lib.escape(title)
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
    /* 结尾视频链接：灰色紧凑，与网页版一致 */
    hr:last-of-type {{ margin: 4mm 0 2mm; }}
    hr:last-of-type + p {{ color: #6b7280; line-height: 1.3; margin: 0; }}
    hr:last-of-type + p a {{ color: #6b7280; }}
    .footer {{ margin-top: 8mm; padding-top: 3mm; border-top: 1px solid #e5e7eb;
              color: #9ca3af; font-size: 9pt; break-before: avoid; }}
</style>
</head>
<body>
{body_html}
<div class="footer">生成时间：{now} ｜ 由 B站视频总结工具 (Bili-summary) 自动生成</div>
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


# ---------------------------------------------------------------------------
# 图片导出：后端用 Playwright 驱动系统 Edge（无头）打开 /export 渲染页，
# 用浏览器原生截图能力截取总结卡片。网页 JS 无权截取自身像素，
# html2canvas 一类"重绘"方案会丢列表圆点等细节，勿改回。
# ---------------------------------------------------------------------------

_export_lock = threading.Lock()


def _export_image_filename(title: str, key: str) -> str:
    """图片文件名 = 视频标题（剔除 Windows 非法字符、截断）_总结.png。"""
    safe = re_lib.sub(r'[\\/:*?"<>|\r\n\t]', "_", title).strip(" ._") or key
    return f"{safe[:60]}_总结.png"


def _launch_headless_browser(p):
    """优先用系统 Edge/Chrome，避免要求 playwright install 下载 Chromium。"""
    errors = []
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=True)
        except Exception as exc:
            errors.append(f"{channel}: {exc}")
    try:
        return p.chromium.launch(headless=True)  # 已 playwright install 的兜底
    except Exception as exc:
        raise RuntimeError(
            "无法启动无头浏览器，请确认已安装 Edge 或 Chrome（"
            + "；".join(errors) + f"；chromium: {exc}）")


def _capture_summary_png(page_url: str) -> bytes:
    """无头浏览器打开导出页并截取总结卡片（同步函数，由路由跑在线程池）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch_headless_browser(p)
        try:
            context = browser.new_context(
                viewport={"width": 1024, "height": 800},
                device_scale_factor=2,  # 2 倍分辨率，中文小字不发虚
            )
            page = context.new_page()
            # 卡片字体不含 Inter，拦截 Google Fonts 外网请求避免加载阻塞
            page.route("**://fonts.googleapis.com/**", lambda route: route.abort())
            page.goto(page_url, wait_until="load", timeout=30_000)
            page.wait_for_selector(
                ".js-export-target .nicegui-markdown p, "
                ".js-export-target .nicegui-markdown h1",
                timeout=15_000)
            page.wait_for_timeout(600)  # 等 DOMPurify 消毒与样式应用稳定
            return page.locator(".js-export-target").screenshot()
        finally:
            browser.close()


@ui.page("/export/{key}")
def _export_page(key: str):
    """无头截图专用渲染页：容器与主页阅读区同一套类与 _CUSTOM_CSS、
    同一个 ui.markdown 组件，保证截图排版与网页版完全一致。"""
    with _print_lock:
        source = _export_sources.get(key)
    ui.add_head_html(_CUSTOM_CSS)
    with ui.column().classes("w-full max-w-5xl mx-auto px-8 py-8 gap-4"):
        with ui.card().classes("bili-card summary-body js-export-target w-full "
                               "p-6 no-shadow"):
            ui.markdown(source[1] if source else
                        "⚠️ 导出内容不存在或已过期，请回到主页重新生成。")


@app.get("/export_png")
def _export_png(key: str, request: Request) -> Response:
    """生成并下载总结长图（真实浏览器截图）。"""
    with _print_lock:
        source = _export_sources.get(key)
    if source is None:
        return HTMLResponse("<h3>导出内容不存在或已过期，请回到主页重新生成。</h3>")
    title = source[0]
    try:
        with _export_lock:  # 无头 Edge 同时只起一个，避免并发互相干扰
            png = _capture_summary_png(f"{request.base_url}export/{quote(key)}")
    except ImportError:
        return HTMLResponse(
            "<h3>导出图片需要 Playwright，请先执行：pip install playwright</h3>")
    except Exception as exc:
        return HTMLResponse(f"<h3>生成图片失败：{html_lib.escape(str(exc))}</h3>")
    filename = _export_image_filename(title, key)
    return Response(png, media_type="image/png", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
    })


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


# 断线覆盖层：连接断开时显示中文提示 + 重新加载按钮，
# 并每 3 秒自动探测服务恢复（配合 后台启动.vbs 唤起服务后自动复活页面）
_DISCONNECT_OVERLAY = """
<div id="bili-disconnect"
     style="display:none; position:fixed; inset:0; z-index:99999;
            background:rgba(15,23,42,.55); backdrop-filter:blur(3px);
            align-items:center; justify-content:center;">
  <div style="background:#fff; border-radius:16px; padding:28px 34px; max-width:430px;
              box-shadow:0 20px 50px rgba(0,0,0,.25); text-align:center;">
    <div style="font-size:2.1rem; margin-bottom:8px;">🔌</div>
    <div style="font-weight:700; color:#1e293b; font-size:1.05rem; margin-bottom:6px;">
      与服务器的连接已断开</div>
    <div style="color:#64748b; font-size:.85rem; margin-bottom:18px; line-height:1.6;">
      服务可能已自动退出或正在重启。<br>
      每 2 秒自动探测，恢复后将自动刷新页面；也可双击「后台启动.vbs」唤起服务。</div>
    <button onclick="location.reload()"
            style="background:#fb7299; color:#fff; border:none; border-radius:10px;
                   padding:9px 28px; font-size:.95rem; font-weight:600; cursor:pointer;">
      重新加载</button>
  </div>
</div>
<script>
(function () {
  // 独立健康轮询：NiceGUI 没有 onNiceGuiDisconnect 一类的 JS 钩子
  //（实测 3.16 不生效），原生重连提示也不含"重新加载"入口。
  // 这里每 2 秒探测 /healthz，连续 2 次失败（服务器进程已死，如闲置
  // 自动退出）才弹覆盖层；恢复后自动刷新页面复活。
  var overlay = document.getElementById('bili-disconnect');
  var failures = 0;
  setInterval(function () {
    fetch('/healthz', { cache: 'no-store' }).then(function (r) {
      if (r.ok) {
        if (overlay.style.display === 'flex') location.reload();
        failures = 0;
      } else {
        failures++;
      }
    }).catch(function () { failures++; });
    if (failures >= 2) overlay.style.display = 'flex';
  }, 2000);
})();
</script>
"""


@app.get("/healthz")
def _healthz() -> PlainTextResponse:
    """页面健康轮询端点：进程活着即返回 200。"""
    return PlainTextResponse("ok")


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
    /* 总结正文照搬 GitHub markdown 样式（github-markdown-css）：
       无衬线字体栈、16px/1.5 行高、标题下边框、灰底代码块 */
    .bili-card.summary-body {
        font-size: 16px; line-height: 1.5; color: #1f2328;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                     "Noto Sans SC", "Noto Sans", Helvetica, Arial,
                     sans-serif !important;
        word-wrap: break-word;
    }
    .summary-body h1, .summary-body h2, .summary-body h3, .summary-body h4 {
        font-family: inherit !important; font-weight: 600; line-height: 1.25;
        margin: 24px 0 16px; padding-bottom: .3em;
    }
    .summary-body h1 { font-size: 2em; border-bottom: 1px solid #d0d7de; }
    .summary-body h2 { font-size: 1.5em; border-bottom: 1px solid #d0d7de; }
    .summary-body h3 { font-size: 1.25em; }
    .summary-body h4 { font-size: 1em; }
    .summary-body p { margin: 0 0 16px; }
    .summary-body ul, .summary-body ol { padding-left: 2em; margin: 0 0 16px; }
    .summary-body li { margin-top: .25em; }
    .summary-body li > p { margin-top: .25em; }
    .summary-body strong { font-weight: 600; }
    .summary-body a { color: #0969da; text-decoration: none; }
    .summary-body a:hover { text-decoration: underline; }
    .summary-body code {
        font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo,
                     Consolas, "Liberation Mono", monospace !important;
        font-size: 85%; padding: .2em .4em; margin: 0;
        background: rgba(175, 184, 193, .2); border-radius: 6px;
    }
    .summary-body pre {
        background: #f6f8fa; padding: 16px; border-radius: 6px;
        overflow: auto; margin: 0 0 16px;
    }
    .summary-body pre code { background: none; padding: 0; font-size: 100%; }
    .summary-body blockquote {
        margin: 0 0 16px; padding: 0 1em; color: #656d76;
        border-left: .25em solid #d0d7de;
    }
    .summary-body table {
        border-collapse: collapse; margin: 0 0 16px; display: block;
        width: max-content; max-width: 100%; overflow: auto;
    }
    .summary-body th, .summary-body td {
        border: 1px solid #d0d7de; padding: 6px 13px;
    }
    .summary-body th { font-weight: 600; }
    .summary-body hr { height: .25em; background: #d0d7de; border: 0; margin: 24px 0; }
    /* 结尾视频链接（紧跟最后一个分隔线）：灰色、紧凑行距，弱化为元信息 */
    .summary-body hr:last-of-type { margin: 16px 0 12px; }
    .summary-body hr:last-of-type + p {
        color: #656d76; line-height: 1.3; margin-bottom: 0;
    }
    .summary-body hr:last-of-type + p a { color: #656d76; }
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

# 本地设置（设置弹窗）：字数上限 / 深度思考 / 模型 ID。
# 保存即写盘，任务开始时快照读取——改完不用重启，对下一个任务生效。
SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "ui_settings.json")
_DEFAULT_SETTINGS = {
    "word_limit": 800,  # 总结字数上限；None 表示不限制
    "thinking": True,
    # 文字稿来源：auto=字幕优先回退本地转录（默认）；subtitle=仅B站字幕，
    # 拿不到直接报错；transcribe=跳过字幕，下载音频本地转写
    "text_source": "auto",
    "model_id": os.getenv("MODEL_ID", "gpt-5.6-sol"),
    "base_url": os.getenv("LLM_BASE_URL", "https://api.avemujica.moe/v1"),
    "api_key": os.getenv("LLM_API_KEY", ""),
}


def _load_settings() -> dict:
    """读取本地设置；文件缺失或损坏时回退默认值。
    兼容旧版 thinking_state.json 保存过的思考开关（仅首次迁移）。"""
    settings = dict(_DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as file:
            saved = json.load(file)
        if isinstance(saved, dict):
            for key in settings:
                if key in saved:
                    settings[key] = saved[key]
    except (OSError, ValueError):
        pass
    if not os.path.exists(SETTINGS_PATH):
        try:
            with open(os.path.join(_PROJECT_ROOT, "thinking_state.json"),
                      "r", encoding="utf-8") as file:
                settings["thinking"] = bool(json.load(file).get("enabled"))
        except (OSError, ValueError, AttributeError):
            pass
    return settings


def _save_settings(settings: dict) -> None:
    """保存设置到本地，供下次打开页面恢复。"""
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as file:
            json.dump(settings, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"设置保存失败: {exc}", file=sys.stderr)


# 模型列表缓存：记录查询时用的 (base_url, api_key)，凭证变化后自动重查
_model_options_cache = None  # ((base_url, api_key), models)
_model_options_lock = threading.Lock()


def _cached_model_options(base_url, api_key):
    """返回与当前凭证匹配的缓存模型列表；不匹配返回 None。"""
    with _model_options_lock:
        cache = _model_options_cache
    if cache and cache[0] == (base_url, api_key):
        return cache[1]
    return None


def _model_select_options(models, current):
    """下拉框选项 = 中转站模型列表；已保存的模型不在列表里也保留在最前。"""
    options = list(models or [])
    if current and current not in options:
        options.insert(0, current)
    return options


def _fetch_model_options_async(select, current, base_url, api_key, force=False):
    """凭证匹配且有缓存直接用；否则后台线程查询中转站，成功后回填下拉框。

    查询失败只打日志，下拉框退化为仅显示当前模型（仍可手动输入自定义 ID）。
    """
    global _model_options_cache

    if not force and _cached_model_options(base_url, api_key) is not None:
        return

    def worker():
        global _model_options_cache
        try:
            models = _get_core().list_available_models(
                base_url=base_url, api_key=api_key)
        except Exception as exc:
            print(f"查询模型列表失败（下拉框仅显示当前模型）: {exc}", file=sys.stderr)
            return
        with _model_options_lock:
            _model_options_cache = ((base_url, api_key), models)
        try:
            select.set_options(_model_select_options(models, current))
        except Exception:
            pass  # 浏览器页面已关闭，无需回填

    threading.Thread(target=worker, daemon=True).start()


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
        self.cache_hit = False       # 转录缓存命中（跳过前三步）
        self.subtitle_hit = False    # B站字幕命中（跳过下载与转录两步）
        self.subtitle_time = 0.0
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


def _run_pipeline(state: TaskState, url: str, settings: dict):
    """后台流水线线程：解析 → (缓存判断) → 信息 → 下载 → 转录 → 流式总结 → 落盘。

    所有用户可见的错误以中文写入 state.error，由页面展示。
    """
    try:
        state.update(phase="running", step=1,
                     message="正在启动后端…",
                     error="", started_at=time.time())

        def progress(msg):
            # 取消只在回调检查点生效（与桌面版引擎行为一致）
            if state.cancel_event.is_set():
                raise TaskCancelled()
            state.update(message=msg)

        # bili_core 已无重型依赖（funasr 隔离在子进程），导入秒级完成；
        # 双检锁保留作保护，极端情况下也只阻塞一瞬间
        core = _get_core()
        bvid, p = core.extract_bvid_and_p(url)
        if not bvid:
            state.update(phase="error", error="无效的 B 站视频链接")
            return
        state.update(bvid=bvid, p=p, task_key=f"{bvid}_p{p}")

        # 磁盘缓存命中：转录稿已落盘，跳过前三步直接进入 AI 总结
        cached_title, cached_text = core.load_cached_transcription(bvid, p)
        transcript = ""  # 进入 AI 总结的文字稿（缓存 / B站字幕 / 本地转录 三来源）
        if cached_text:
            transcript = cached_text
            state.update(cache_hit=True, step=4,
                         title=cached_title or bvid,
                         stream_text="", message="已命中转录缓存，正在重新生成总结…")

            # 缓存命中跳过了第 1 步，视频信息卡会缺内容——后台补拉一次
            # （不阻塞已开始的 AI 总结，拉到后由渲染定时器自动补显）
            def _backfill_video_info():
                try:
                    info = core.get_video_info(bvid)
                    t = info["title"]
                    if len(info.get("pages", [])) > 1 and 1 <= p <= len(info["pages"]):
                        t = f"{t} - {info['pages'][p - 1]['part']}"
                    state.update(video_info=info, title=t,
                                 cover_src=_fetch_cover_base64(info.get("pic", "")))
                except Exception as exc:
                    print(f"[缓存命中] 补拉视频信息失败: {exc}", file=sys.stderr)

            threading.Thread(target=_backfill_video_info, daemon=True).start()
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

            # 第 1.5 步：按「文字稿来源」设置决定字幕/转录路线
            # （auto=字幕优先回退转录；subtitle=仅字幕；transcribe=跳过字幕）
            text_source = settings.get("text_source", "auto")
            subtitle_text = None
            sub_started = None
            if text_source != "transcribe":
                state.update(message="正在尝试读取B站字幕...")
                sub_started = time.time()
                subtitle_text = core.fetch_subtitle_text(bvid, p, progress)
                # fetch_subtitle_text 内部吞掉所有异常，取消在这里补检一次
                if state.cancel_event.is_set():
                    raise TaskCancelled()
            if subtitle_text:
                transcript = subtitle_text
                core.save_transcription(bvid, state.title, subtitle_text, p)
                state.update(
                    subtitle_hit=True, subtitle_time=time.time() - sub_started,
                    step=4, stream_text="",
                    message="已读取B站字幕，跳过下载与转录，正在调用 AI 模型…")
            elif text_source == "subtitle":
                state.update(
                    phase="error",
                    error="未获取到B站字幕（该视频无字幕，或AI字幕需在设置中扫码登录），"
                          "可改为「自动」或「使用本地转录」后再试")
                return
            else:
                # 第 2 步：下载音频
                state.update(step=2, message="正在下载音频...")
                started = time.time()
                title, audio_path = core.download_audio(bvid, p, progress)
                state.update(title=title, download_time=time.time() - started)

                # 第 3 步：等待 ASR 工作进程（预热未完成时）并转录。
                # 转录在 asr_worker 子进程执行，主进程全程不加载 torch
                state.update(step=3, message="正在准备语音识别...")
                worker = get_asr_worker()
                if worker.status() in ("starting", "loading"):
                    wait_start = time.time()
                    while worker.status() in ("starting", "loading"):
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
                text = worker.transcribe(audio_path, progress)
                state.update(transcribe_time=time.time() - started)
                transcript = text

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
            transcript,
            progress,
            enable_thinking=settings["thinking"],
            model_id=settings["model_id"],
            word_limit=settings["word_limit"],
            base_url=settings["base_url"],
            api_key=settings["api_key"],
        ):
            full_summary += chunk
            state.update(stream_text=full_summary)

        summarize_time = time.time() - summarize_started
        core.save_results(bvid, state.title, transcript, full_summary, p)
        if state.subtitle_hit:
            timing = {
                "字幕获取": state.subtitle_time,
                "AI 总结": summarize_time,
                "总耗时": state.subtitle_time + summarize_time,
            }
        else:
            timing = {
                "音频下载": state.download_time,
                "音频转录": state.transcribe_time,
                "AI 总结": summarize_time,
                "总耗时": state.download_time + state.transcribe_time + summarize_time,
            }
        # 展示/导出用统一版式（标题开头、链接收尾），与落盘文件同源
        final_md = core.format_summary_markdown(
            state.title, full_summary,
            core._resolve_bili_video_url(state.url, bvid, p))
        state.update(
            phase="done", step=5, final_summary=final_md, timing=timing,
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

    # ---- 扫码登录B站弹窗：二维码由后端调B站 passport 接口生成 ----
    qr_state = {"key": None, "timer": None}

    def _qr_png_data_uri(url):
        """登录 URL 转二维码 PNG 的 data URI（本地生成，无外网依赖）。"""
        import io

        import qrcode
        buf = io.BytesIO()
        qrcode.make(url, box_size=8, border=2).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(
            buf.getvalue()).decode("utf-8")

    def _stop_qr_timer():
        if qr_state["timer"] is not None:
            qr_state["timer"].deactivate()
            qr_state["timer"] = None

    async def _poll_qrcode():
        if not login_dialog.value or qr_state["key"] is None:
            _stop_qr_timer()
            return

        def _poll():
            return _get_core().poll_bili_login_qrcode(qr_state["key"])

        try:
            result = await run.io_bound(_poll)
        except Exception as exc:
            qr_status.text = f"查询登录状态失败：{exc}"
            return
        status = result.get("status")
        if status == "waiting":
            qr_status.text = "等待扫码（手机B站 App → 扫一扫）…"
        elif status == "scanned":
            qr_status.text = "已扫码，请在手机上确认登录…"
        elif status == "confirmed":
            _stop_qr_timer()
            qr_status.text = "✅ 登录成功，AI 字幕已启用"
            ui.notify("B站登录成功，AI 字幕功能已启用。", type="positive")
            rendered["bili"] = None  # 让下一拍刷新登录徽标/按钮
            login_dialog.close()
        elif status == "expired":
            _stop_qr_timer()
            qr_status.text = "二维码已过期，请点击「刷新二维码」。"
        else:
            qr_status.text = "登录状态异常，请刷新二维码重试。"

    async def _new_qrcode():
        qr_status.text = "正在获取二维码…"

        def _generate():
            return _get_core().generate_bili_login_qrcode()

        try:
            key, url = await run.io_bound(_generate)
            data_uri = await run.io_bound(_qr_png_data_uri, url)
        except Exception as exc:
            qr_status.text = f"获取二维码失败：{exc}"
            return
        qr_state["key"] = key
        qr_img.set_source(data_uri)
        _stop_qr_timer()
        qr_state["timer"] = ui.timer(2.0, _poll_qrcode)
        qr_status.text = "等待扫码（手机B站 App → 扫一扫）…"

    async def open_login_dialog():
        login_dialog.open()
        await _new_qrcode()

    with ui.dialog() as login_dialog, ui.card().classes("w-[340px] gap-3"):
        ui.label("📱 扫码登录B站").classes("text-base font-semibold text-slate-700")
        ui.label("用手机B站 App 扫一扫。登录后 AI 字幕与受限下载自动启用；"
                 "凭证只保存在本机 bili_cookies.txt。") \
            .classes("text-xs text-slate-400 leading-relaxed")
        qr_img = ui.image().classes("w-52 h-52 mx-auto rounded-lg "
                                    "border border-slate-100")
        qr_status = ui.label("正在获取二维码…") \
            .classes("text-xs text-slate-500 text-center")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("刷新二维码", on_click=_new_qrcode) \
                .props("outline no-caps color=grey-7")
            ui.button("关闭", on_click=login_dialog.close) \
                .props("flat no-caps color=grey-7")

    # ---- 设置弹窗：总结字数 / 深度思考 / 模型 ID ----
    # 保存即写盘，任务开始时快照读取，改完不用重启程序
    settings = _load_settings()
    with ui.dialog() as settings_dialog, ui.card().classes("w-[360px] gap-2"):
        ui.label("⚙️ 设置").classes("text-base font-semibold text-slate-700")
        base_url_input = (ui.input("API 地址", value=settings["base_url"])
                          .props("dense outlined"))
        # API Key 密文显示，避免旁人窥屏；值与 .env 中 LLM_API_KEY 相同
        api_key_input = (ui.input("API Key", value=settings["api_key"],
                                  password=True)
                         .props("dense outlined"))
        # 下拉列表按上方 API 地址/Key 实时查询；with_input + new_value_mode
        # 允许直接输入列表外的自定义模型 ID（回车确认）
        model_select = (ui.select(
            _model_select_options(
                _cached_model_options(settings["base_url"], settings["api_key"]),
                settings["model_id"]),
            value=settings["model_id"], label="模型 ID",
            with_input=True, new_value_mode="add")
            .props("dense outlined"))
        _fetch_model_options_async(model_select, settings["model_id"],
                                   settings["base_url"], settings["api_key"])
        limit_input = (ui.number("总结字数上限", value=settings["word_limit"],
                                 step=100, placeholder="清空则不限制字数")
                       .props("dense outlined clearable"))
        thinking_switch = ui.switch("🧠 深度思考（更慢但更详细）",
                                    value=settings["thinking"])
        # 文字稿来源：决定流水线取字幕还是本地转录（缓存命中不受影响）
        ui.label("文字稿来源").classes("text-sm text-slate-600")
        text_source_toggle = (ui.toggle(
            {"subtitle": "使用B站字幕", "auto": "自动", "transcribe": "使用本地转录"},
            value=settings.get("text_source", "auto"))
            .props("dense no-caps")
            .classes("w-full flex-wrap"))
        ui.label("自动=优先B站字幕、拿不到回退本地转录；使用B站字幕=只用字幕，"
                 "无字幕时报错；使用本地转录=跳过字幕。已生成过的视频直接复用缓存文字稿。"
                 ).classes("text-xs text-slate-400")
        ui.label("模型列表按 API 地址/Key 查询；改了凭证保存后会自动刷新。"
                 "保存后对下一个任务生效。").classes("text-xs text-slate-400")

        ui.separator().classes("my-1")
        # B站扫码登录：登录后整行隐藏，凭证过期后会随徽标一起回来
        with ui.row().classes("w-full items-center justify-between no-wrap") \
                as settings_login_row:
            ui.label("B站扫码登录（启用 AI 字幕）") \
                .classes("text-sm text-slate-600")
            ui.button("扫码登录", on_click=open_login_dialog) \
                .props("flat no-caps").classes("no-shadow px-3")

        def on_save_settings():
            word_limit = int(limit_input.value) if limit_input.value else None
            if word_limit is not None and word_limit < 1:
                ui.notify("字数上限需为正整数。", type="warning")
                return
            base_url = (base_url_input.value or "").strip().rstrip("/")
            if base_url and not base_url.startswith(("http://", "https://")):
                ui.notify("API 地址需以 http:// 或 https:// 开头。", type="warning")
                return
            old_creds = (settings["base_url"], settings["api_key"])
            settings.update(
                word_limit=word_limit,
                thinking=bool(thinking_switch.value),
                text_source=text_source_toggle.value or "auto",
                model_id=(str(model_select.value or "").strip()
                          or _DEFAULT_SETTINGS["model_id"]),
                base_url=base_url or _DEFAULT_SETTINGS["base_url"],
                api_key=(api_key_input.value or "").strip()
                        or _DEFAULT_SETTINGS["api_key"],
            )
            _save_settings(settings)
            # API 地址/Key 变了 → 强制重查模型列表并回填下拉框
            if (settings["base_url"], settings["api_key"]) != old_creds:
                _fetch_model_options_async(
                    model_select, settings["model_id"],
                    settings["base_url"], settings["api_key"], force=True)
            settings_dialog.close()
            ui.notify("设置已保存，下一个任务生效。", type="positive")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=settings_dialog.close) \
                .props("outline no-caps color=grey-7")
            ui.button("保存", on_click=on_save_settings) \
                .props("unelevated no-caps")

    # 品牌色注入 Quasar 主题：按钮/复选框/输入框焦点色自动继承
    ui.colors(primary=BILI_PINK, secondary=BILI_BLUE)
    ui.add_head_html(_CUSTOM_CSS)
    ui.add_body_html(_DISCONNECT_OVERLAY)

    # ---- 左侧工具面板 ----
    with ui.left_drawer(top_corner=True, bottom_corner=True) \
            .classes("w-[340px] px-4 py-5 gap-3"):
        with ui.column().classes("w-full gap-1 px-1 pb-1"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("🎬 B站视频总结").classes("bili-title")
                ui.button(icon="settings", on_click=settings_dialog.open) \
                    .props("flat round dense size=sm color=grey-7")
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
            asr_badge = ui.label().classes("text-xs text-slate-400")
            # B站登录提示：未登录时告知入口（扫码按钮在 ⚙️ 设置里），登录后隐藏
            bili_badge = ui.label().classes("text-xs text-slate-400")

        if not settings["api_key"]:
            ui.label("⚠️ 未配置 API Key，AI 总结不可用，请在 ⚙️ 设置或 .env 中配置。") \
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
            # 必须用原生 <a target=_blank> 链接：之前 ui.open() 通过 websocket
            # 下行 window.open，脱离用户手势上下文会被弹窗拦截器静默拦截，
            # 表现为点击打印按钮无反应。链接在任务完成时创建（见 refresh）。
            print_slot = ui.row().classes("gap-2 no-wrap items-center")
            print_slot.set_visibility(False)
        with ui.card().classes("bili-card summary-body w-full "
                               "p-6 min-h-[300px] no-shadow"):
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
        # 任务开始时快照当前设置，弹窗里的后续修改不影响进行中的任务
        threading.Thread(
            target=_run_pipeline,
            args=(state, url, dict(settings)),
            daemon=True,
        ).start()

    def on_cancel():
        if state.phase == "running":
            state.cancel_event.set()
            ui.notify("正在取消当前任务，请稍候…")
        else:
            ui.notify("当前没有正在处理的任务。")

    # ------------------------------------------------------------------
    # 周期渲染：从 TaskState 快照刷新 UI（内容变化时才更新元素）
    # ------------------------------------------------------------------
    rendered = {"signature": None, "dot_cls": [None] * 4,
                "line_done": [None] * 3, "phase": None, "running": None,
                "asr": None, "bili": None, "cover": None, "print_key": None}

    async def refresh():
        with state.lock:
            snap = {
                "phase": state.phase, "step": state.step, "message": state.message,
                "video_info": state.video_info, "cover_src": state.cover_src,
                "timing": state.timing, "error": state.error,
                "cache_hit": state.cache_hit, "subtitle_hit": state.subtitle_hit,
                "title": state.title,
                "task_key": state.task_key, "url": state.url,
                "stream_text": state.stream_text, "final_summary": state.final_summary,
                "sound_pending": state.sound_pending,
            }

        if snap["sound_pending"]:
            state.update(sound_pending=False)
            ui.run_javascript(_SOUND_JS)

        signature = (
            snap["phase"], snap["step"], snap["message"], snap["cover_src"],
            snap["timing"], snap["error"], snap["cache_hit"],
            snap["video_info"] is not None,  # 缓存命中补拉的视频信息要触发重渲染
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
            elif snap["subtitle_hit"] and step_num in (2, 3):
                cls = "step-dot-skip"   # 字幕命中：获取信息正常跑，下载/转录跳过
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

        # 打印/导出链接：任务完成时注册。用 task_key 作为键，
        # 同任务重跑时覆盖为最新结果。
        if snap["phase"] == "done" and snap.get("final_summary"):
            if rendered["print_key"] != snap["task_key"]:
                key = snap["task_key"]
                with _print_lock:
                    _print_pages[key] = _build_print_html(
                        snap.get("title") or "视频总结",
                        snap["final_summary"])
                    _export_sources[key] = (snap.get("title") or "视频总结",
                                            snap["final_summary"])
                    for old_key in list(_print_pages)[:-20]:
                        _print_pages.pop(old_key, None)
                    for old_key in list(_export_sources)[:-20]:
                        _export_sources.pop(old_key, None)
                print_slot.clear()
                with print_slot:
                    ui.link("🖨️ 打印总结", target=f"/print?key={key}") \
                        .props("target=_blank") \
                        .classes("no-decoration text-xs text-slate-500 "
                                 "border border-slate-200 rounded-md "
                                 "px-3 py-1.5 bg-white hover:bg-slate-50")
                    # 导出图片：后端无头浏览器真截图，附件下载不跳转页面
                    ui.link("🖼️ 导出图片", target=f"/export_png?key={key}") \
                        .classes("no-decoration text-xs text-slate-500 "
                                 "border border-slate-200 rounded-md "
                                 "px-3 py-1.5 bg-white hover:bg-slate-50")
                rendered["print_key"] = key
        print_slot.set_visibility(snap["phase"] == "done")

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
            status = get_asr_worker().status()
            if status in ("starting", "loading"):
                asr_text = "⏳ 识别引擎加载中（不阻塞下载）"
            elif status == "ready":
                asr_text = ""  # 就绪后不再占侧栏，只在异常状态提示
            elif status in ("error", "dead"):
                asr_text = "⚠️ 识别引擎异常，下次转写时自动重启"
            else:
                asr_text = "🔓 识别引擎待命"
        if rendered["asr"] != asr_text:
            asr_badge.text = asr_text
            asr_badge.set_visibility(bool(asr_text))
            rendered["asr"] = asr_text
        # B站登录：后端未就绪时登录态未知，不显示误导性提示；
        # 只有确认未登录才提示入口并显示设置里的扫码行，登录后全部隐去
        logged_in = None if _core is None else _core.bili_login_ready()
        bili_text = "🔓 B站未登录（⚙️ 设置里扫码可启用 AI 字幕）" \
            if logged_in is False else ""
        if rendered["bili"] != bili_text:
            bili_badge.text = bili_text
            bili_badge.set_visibility(bool(bili_text))
            settings_login_row.set_visibility(logged_in is False)
            rendered["bili"] = bili_text

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
    # 启动时同步预热浏览器 cookie：必须赶在启动脚本自动打开浏览器之前
    # 完成（Edge/Chrome 运行中会锁 cookie 库导致提取失败），最多阻塞 10 秒
    import bili_cookies
    bili_cookies.warm_from_browser()

    # 进程启动即开始导入 bili_core + 预热 ASR（不等首个页面连接）：
    # 重型导入（funasr/torch，约 30 秒）尽量提前，缩短任务开始时
    # 可能等待"后端初始化"的时间
    _start_asr_preload()

    headless = os.environ.get("BILI_UI_HEADLESS") == "1"
    port = int(os.environ.get("BILI_UI_PORT", str(DEFAULT_PORT)))
    # reload=False：生产入口，避免文件监视器带来的双进程与重载副作用
    ui.run(
        title="B站视频总结工具",
        port=port,
        reload=False,
        show=not headless,
        favicon="🎬",
        language="zh-CN",  # 原生断线/重连提示使用中文
    )
