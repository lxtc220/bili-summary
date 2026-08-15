import streamlit as st

# 1. 立即设置页面配置，减少白屏等待感
st.set_page_config(
    page_title="B站视频总结工具",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded"
)

import time
import os
import sys
import threading
import datetime
import streamlit.components.v1 as components
from dotenv import load_dotenv

# 读取 .env（bili_core 内部也会读；这里提前读是为了侧边栏的
# LLM_API_KEY 校验不依赖 bili_core 加载完成）
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# 进程级标志位：保证 ASR 预加载线程在整个 Streamlit 进程生命周期内只起一次。
# （模块级变量在 Streamlit 进程存活期间只初始化一次，所有 session 共享，
# 比 per-session 的 session_state 更适合做"进程级只执行一次"的守护。）
_asr_preload_triggered = False
_monitor_started = False

# bili_core 懒加载：首屏渲染不再等待 funasr/torch 等重型依赖 import，
# 全部挪到后台预热线程里完成（bili_core 内部仍保持 funasr 最先加载，
# 规避 Windows 上三方库加载顺序导致的段错误）。_backend_ready 只表示
# bili_core 已 import 完成（之后可安全调用 _get_core），与 ASR 模型的
# 加载进度无关——模型加载在后台并行进行，处理流程只到转录步骤才等它。
_core = None
_backend_ready = False
_backend_lock = threading.Lock()


def _get_core():
    """首次调用时 import bili_core 并缓存（线程安全）。若后台线程正在
    import，这里会阻塞等待其完成。"""
    global _core
    if _core is None:
        with _backend_lock:
            if _core is None:
                import bili_core
                _core = bili_core
    return _core


def _asr_preload_worker():
    """后台预热线程：先 import bili_core，再预加载 ASR 模型。

    _backend_ready 在 import 完成后立即置位（"可以开始处理"的门槛），
    模型加载继续在本线程后台进行，成功/失败都不影响处理流程——
    若加载未完成，用户点「开始处理」仍能正常走到下载音频，到转录
    步骤时 transcribe_audio 内部会阻塞等待模型就绪（bili_core 的
    preload_asr_model 双检锁保证只加载一次）。
    """
    global _backend_ready
    try:
        _get_core()
    except Exception as e:
        print(f"bili_core 导入失败: {e}", file=sys.stderr)
    # 无论导入成功与否都放行：失败时真实错误会在用户点击时直接暴露，
    # 避免 _backend_ready 永远为 False 导致按钮被永久拦截
    _backend_ready = True
    try:
        _get_core().preload_asr_model()
    except Exception as e:
        print(f"ASR 预加载失败（首次转写时会自动重试）: {e}", file=sys.stderr)

# 自动关闭功能：如果没有活跃连接，则关闭后台
def monitor_sessions():
    """后台监控线程：如果 30 分钟内没有任何网页连接，则自动关闭服务器"""
    from streamlit.runtime import get_instance
    time.sleep(60) # 启动宽限期增加到 60 秒
    
    inactive_count = 0
    while True:
        try:
            runtime = get_instance()
            # 获取当前活跃的 Session 列表
            sessions = runtime._session_mgr.list_active_sessions()
            
            if not sessions:
                inactive_count += 1
                if inactive_count >= 900: # 连续 900 次检测到无连接（约 30 分钟），则关闭
                    print("检测到长时间无连接，正在自动退出后台进程...")
                    runtime.stop()
                    os._exit(0)
            else:
                inactive_count = 0 # 重置计数器
        except Exception:
            pass
        time.sleep(2)

# 只在第一次运行时启动监控线程（模块级标志，进程内只起一次；
# 原来的 per-session 守卫会导致每个浏览器 tab 都起一个线程）
if not _monitor_started:
    _monitor_started = True
    thread = threading.Thread(target=monitor_sessions, daemon=True)
    thread.start()


# 不在首屏后台预加载 ASR。
# FunASR/torch 会加载 Windows 原生 DLL，后台线程与 Streamlit 首屏并行初始化
# 时可能触发 msvcp140.dll/arrow.dll 访问冲突，导致整个 Python 进程退出，浏览器
# 随后显示 Connection error。首次转写时再由主线程按既有顺序加载，页面启动更稳。


def _processing_task_key(bvid, p):
    return f"{bvid}_p{p}"


def _clear_transient_processing_state():
    """只清理本次处理的临时状态，保留页面任务身份和已完成结果。"""
    transient_keys = [
        'video_info',
        'title',
        'audio_path',
        'text',
        'download_time',
        'transcribe_time',
        'current_summary',
        'final_summary',
        'timing',
        'cached_summary',
        'cache_hit',
        'play_completion_sound',
        'print_requested',
    ]
    for key in transient_keys:
        st.session_state.pop(key, None)


def play_completion_sound():
    """使用 Web Audio API 在浏览器端合成一个"叮咚"完成音，无需外部音频文件。"""
    components.html(
        """
<script>
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
        // 两声上扬"叮咚"音，柔和提示
        tone(880, 0, 0.25, 0.25);
        tone(1320, 0.18, 0.45, 0.25);
    } catch (e) {
        console.warn('播放提示音失败:', e);
    }
})();
</script>
        """,
        height=0,
    )


def _build_print_html(title, summary_md):
    """
    把总结 markdown 组装成完整的 A4 打印页 HTML（浏览器打印方案）。

    调研结论：md 打印成 A4 的主流做法是浏览器打印（@media print + @page A4
    + window.print()），零依赖、中文渲染好；pandoc/WeasyPrint 等需重依赖
    （LaTeX 数 GB / Windows 需 GTK），不适用于本项目。总结先经 Python
    markdown 库转成 HTML 再套 A4 页面样式。LLM 输出不可信，转换结果会
    剥离 script/iframe 等活 HTML 与事件属性，防止注入。
    """
    import html as html_lib
    import re as re_lib
    import markdown as md_lib

    # markdown → HTML（表格/代码块/换行扩展；总结常用的 # ** - 1. 等语法全覆盖）
    body_html = md_lib.markdown(
        summary_md,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    # 消毒：剥离 LLM 输出里可能携带的活 HTML 标签（含成对内容）
    body_html = re_lib.sub(
        r"<(script|iframe|style|object|embed|link|meta)\b[^>]*>.*?</\1>"
        r"|<(script|iframe|style|object|embed|link|meta)\b[^>]*/?>",
        "",
        body_html,
        flags=re_lib.IGNORECASE | re_lib.DOTALL,
    )
    # 剥离残留的事件属性与 javascript: 链接
    body_html = re_lib.sub(
        r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        body_html,
        flags=re_lib.IGNORECASE,
    )
    body_html = re_lib.sub(r"javascript:", "", body_html, flags=re_lib.IGNORECASE)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    url = st.session_state.get('url', '')
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
</body>
</html>"""


def _trigger_print_summary():
    """
    弹出浏览器打印对话框，打印 A4 排版的总结（"一次性 flag + 渲染"模式，
    与 play_completion_sound 相同，避免每次 rerun 重复弹打印框）。

    注入通道：components.html（其 iframe 的 srcdoc 会完整包含传入的 HTML，
    含 script 标签；sandbox 含 allow-scripts + allow-modals）。iframe 内
    window.print() 只打印 iframe 自身文档（即排版好的总结页），不影响页面
    其余 UI。st.markdown 注入 iframe 的方案不可用（iframe 会被前端清洗）。
    """
    title = st.session_state.get('title') or '视频总结'
    summary_md = st.session_state.get('final_summary', '')
    print_html = _build_print_html(title, summary_md)
    components.html(
        print_html + "\n<script>window.print();</script>",
        height=0,
    )


st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');

    /* 压低 Streamlit 原生 chrome，只保留侧边栏折叠控制 */
    [data-testid="stDecoration"] {
        display: none !important;
    }
    header[data-testid="stHeader"] {
        background: transparent;
        border-bottom: none;
        box-shadow: none;
        height: 2.6rem;
    }
    div[data-testid="stToolbar"] {
        top: 0.25rem;
        right: 0.75rem;
    }
    div[class="stDeployButton"] {
        display: none !important;
    }
    span[data-testid="stMainMenu"] {
        display: none !important;
    }
    footer {
        display: none !important;
    }
    [data-testid="collapsedControl"] {
        top: 0.5rem;
        left: 0.65rem;
    }
    
    /* 设定整体背景为动态渐变或高级纯色 */
    .stApp {
        background: linear-gradient(135deg, #fdfbfb 0%, #ebedee 100%);
        font-family: 'Inter', system-ui, -apple-system, sans-serif;
    }

    .block-container {
        padding-top: 0.8rem;
        padding-bottom: 2rem;
        max-width: 95%; /* 占满屏幕更多空间 */
    }
    
    /* 主标题高级感 */
    .main-header {
        font-size: 2.2rem;
        font-weight: 800;
        background: linear-gradient(135deg, #fb7299 0%, #00aeec 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 1.5rem;
        text-align: center;
        letter-spacing: -0.5px;
        padding-top: 1rem;
    }
    
    /* 侧边栏整体样式 */
    [data-testid="stSidebar"] {
        background-color: rgba(255, 255, 255, 0.6);
        backdrop-filter: blur(20px);
        -webkit-backdrop-filter: blur(20px);
        border-right: 1px solid rgba(255, 255, 255, 0.4);
    }

    /* 输入框样式 */
    .stTextInput > div > div > input {
        border-radius: 12px;
        border: 1px solid #e2e8f0;
        padding: 0.75rem 1rem;
        transition: all 0.3s ease;
        background: rgba(255, 255, 255, 0.9);
        font-size: 1rem;
    }
    .stTextInput > div > div > input:focus {
        border-color: #fb7299;
        box-shadow: 0 0 0 3px rgba(251, 114, 153, 0.2);
    }

    /* 按钮样式 */
    .stButton > button {
        border-radius: 12px;
        font-weight: 600;
        padding: 0.5rem 1rem;
        background: linear-gradient(135deg, #fb7299 0%, #00aeec 100%);
        color: white;
        border: none;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
        width: 100%;
        box-shadow: 0 4px 15px rgba(251, 114, 153, 0.3);
    }
    .stButton > button:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(251, 114, 153, 0.4);
        color: white;
    }
    .stButton > button:active {
        transform: translateY(0);
    }
    
    /* 内容卡片玻璃拟态效果 */
    .summary-box, .progress-section, .video-info-card {
        background: rgba(255, 255, 255, 0.85);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border: 1px solid rgba(255, 255, 255, 0.6);
        border-radius: 20px;
        padding: 1.5rem;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.04);
        margin-bottom: 1.5rem;
    }
    
    .summary-box:hover, .progress-section:hover, .video-info-card:hover {
        box-shadow: 0 15px 35px rgba(0, 0, 0, 0.08);
        transform: translateY(-2px);
    }
    
    /* 步骤卡片 */
    .step-card {
        padding: 1rem 1.25rem;
        border-radius: 16px;
        margin-bottom: 0.8rem;
        transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
        display: flex;
        align-items: center;
        gap: 0.5rem;
        font-weight: 600;
        font-size: 1.05rem;
    }
    
    .step-pending {
        background: rgba(243, 244, 246, 0.7);
        border: 1px solid rgba(229, 231, 235, 0.8);
        color: #6b7280;
    }
    
    .step-running {
        background: linear-gradient(135deg, #00aeec 0%, #0077ff 100%);
        border: none;
        color: white;
        box-shadow: 0 8px 20px rgba(0, 174, 236, 0.3);
        transform: scale(1.02);
    }
    
    .step-completed {
        background: linear-gradient(135deg, #10b981 0%, #059669 100%);
        border: none;
        color: white;
        box-shadow: 0 8px 20px rgba(16, 185, 129, 0.2);
    }

    .step-skipped {
        background: rgba(243, 244, 246, 0.7);
        border: 1px dashed rgba(156, 163, 175, 0.8);
        color: #9ca3af;
    }

    .timing-item {
        display: flex;
        justify-content: space-between;
        padding: 0.75rem 0;
        border-bottom: 1px dashed rgba(0,0,0,0.08);
        font-size: 0.95rem;
        color: #4b5563;
    }
    
    .timing-item:last-child {
        border-bottom: none;
        font-weight: 700;
        color: #111827;
        font-size: 1.05rem;
        margin-top: 0.5rem;
    }
    
    .status-badge {
        display: inline-flex;
        align-items: center;
        padding: 0.4rem 1.2rem;
        border-radius: 999px;
        font-size: 0.9rem;
        font-weight: 700;
        letter-spacing: 0.5px;
    }
    
    .badge-success {
        background: rgba(16, 185, 129, 0.1);
        color: #059669;
        border: 1px solid rgba(16, 185, 129, 0.2);
    }
    
    .badge-warning {
        background: rgba(245, 158, 11, 0.1);
        color: #d97706;
        border: 1px solid rgba(245, 158, 11, 0.2);
    }
    
    .badge-idle {
        background: rgba(107, 114, 128, 0.1);
        color: #4b5563;
        border: 1px solid rgba(107, 114, 128, 0.2);
    }
    
    .section-title {
        font-size: 1.35rem;
        font-weight: 700;
        color: #111827;
        margin-bottom: 1.25rem;
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    
    /* markdown 内容优化 */
    .summary-box h1, .summary-box h2, .summary-box h3 {
        color: #1f2937;
        margin-top: 1rem;
        margin-bottom: 0.5rem;
        font-weight: 700;
    }
    .summary-box p {
        line-height: 1.7;
        color: #374151;
        font-size: 1.05rem;
        margin-bottom: 1rem;
    }
    .summary-box ul {
        margin-top: 0.5rem;
        margin-bottom: 1rem;
    }
    .summary-box li {
        margin-bottom: 0.4rem;
        color: #374151;
        line-height: 1.6;
    }
    
    /* 视频信息卡片强化 */
    .video-info-card h3 {
        margin-top: 1rem;
        margin-bottom: 0.5rem;
        font-size: 1.1rem;
        color: #111827;
    }
    .video-info-card p {
        color: #6b7280;
        font-size: 0.95rem;
    }
    
    /* 封面图圆角 */
    [data-testid="stImage"] img {
        border-radius: 12px;
        box-shadow: 0 4px 10px rgba(0,0,0,0.08);
    }

    hr {
        border-color: rgba(0,0,0,0.06);
        margin: 1.5rem 0;
    }
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown('<h1 class="main-header">🎬 B站视频总结</h1>', unsafe_allow_html=True)
    
    url = st.text_input(
        "输入 B 站视频链接",
        placeholder="https://www.bilibili.com/video/..."
    )
    
    # 验证 API 密钥（直接读环境变量，不依赖 bili_core 加载完成）
    if not os.getenv("LLM_API_KEY"):
        st.warning("⚠️ 未配置 LLM_API_KEY，AI 总结功能将不可用。请在 .env 文件中配置密钥。")

    # AI 思考模式开关：开启后总结更深入但更慢（思维链过程不在界面展示）。
    # 默认开启，追求总结质量；取消勾选可关闭以加快速度。
    enable_thinking = st.checkbox(
        "🧠 深度思考模式",
        value=True,
        help="开启后 AI 会先进行思维链推理再总结，质量更高但耗时更长（适用于复杂/长视频）"
    )
    st.session_state['enable_thinking'] = enable_thinking

    # 处理中（step 1-4）禁用按钮：防止重复点击把 step 重置回 1，
    # 导致下载/转录被并发重复执行（多个 yt-dlp 抢同一文件会卡死）。
    # 预热中点击后（回调阻塞等待 bili_core import / 模型加载时 step 仍为 0），
    # 通过 _submitted 标记保持按钮禁用，直到流程推进（step 1-4 接管）或
    # 失败清除标记（step 归 0 时恢复可点）。
    # 自愈：若 _submitted 残留为 True 但 step 仍是 0（点击那次运行被刷新/断线/
    # 自动刷新事件中断，没走到设置 step 的代码），说明是上次中断留下的死标记，
    # 立即清除，避免按钮永久灰掉、页面看起来卡死。
    _step_now = st.session_state.get('step', 0)
    if st.session_state.get('_submitted', False) and _step_now == 0:
        st.session_state['_submitted'] = False
    _submitted = st.session_state.get('_submitted', False)
    _disabled = _step_now in (1, 2, 3, 4) or (_step_now == 0 and _submitted)
    if st.button("开始处理", type="primary", use_container_width=True, disabled=_disabled):
        # 仅拦截 bili_core 尚未 import 完成的短暂窗口（首次访问约 10-30 秒，
        # 进程内只发生一次）：此时 _get_core() 会阻塞，直接用 spinner 提示
        # 并等待 import 完成，一次点击全程有效，不白点。ASR 模型是否加载
        # 完成完全不参与门控——视频信息获取、音频下载都不依赖模型，只有
        # 到转录步骤（step 3）才等待模型就绪。
        if not _backend_ready:
            # import 只有进程内一次（约 10-30 秒）：阻塞等待期间用 spinner
            # 明确告知用户在等什么，完成后自动继续，一次点击全程有效
            with st.spinner("⏳ 正在加载核心组件（首次运行约 10-30 秒），加载完成后将自动开始处理…"):
                _get_core()
        st.session_state['_submitted'] = True
        try:
            bvid, p = _get_core().extract_bvid_and_p(url)
            if not bvid:
                st.session_state['_submitted'] = False
                st.error("❌ 无效的 B 站视频链接")
            else:
                task_key = _processing_task_key(bvid, p)
                last_completed_key = st.session_state.get('last_completed_key')
                same_completed_task = (
                    last_completed_key == task_key
                    and st.session_state.get('step', 0) >= 5
                    and 'final_summary' in st.session_state
                )

                if same_completed_task:
                    st.session_state['url'] = url
                    st.session_state['bvid'] = bvid
                    st.session_state['p'] = p
                    st.session_state['task_key'] = task_key
                    st.session_state.pop('active_task_key', None)
                    st.session_state['step'] = 5
                    st.info("这是同一个页面，直接复用已有结果，不再重复处理。")
                    st.rerun()

                # 磁盘缓存命中：该 BV 号之前处理过，转录稿已落盘，跳过「获取信息 /
                # 下载 / 转录」三步，直接进入第 4 步重新进行 AI 总结。
                cached_title, cached_text = _get_core().load_cached_transcription(bvid, p)
                if cached_text:
                    _clear_transient_processing_state()
                    st.session_state['url'] = url
                    st.session_state['bvid'] = bvid
                    st.session_state['p'] = p
                    st.session_state['task_key'] = task_key
                    st.session_state['active_task_key'] = task_key
                    # 标题解析失败时退回到 BV 号，保证 AI 总结时标题非空
                    st.session_state['title'] = cached_title or bvid
                    st.session_state['text'] = cached_text
                    st.session_state['cache_hit'] = True
                    st.session_state['cached_summary'] = True
                    st.session_state['step'] = 4
                    st.rerun()

                _clear_transient_processing_state()
                st.session_state['url'] = url
                st.session_state['bvid'] = bvid
                st.session_state['p'] = p
                st.session_state['task_key'] = task_key
                st.session_state['active_task_key'] = task_key
                st.session_state['step'] = 1
                st.rerun()
        except Exception as e:
            st.session_state['_submitted'] = False
            st.error(f"❌ 处理失败: {e}")

    # ASR 引擎状态指示器：让用户看到引擎处于哪个阶段。
    # 注意：这里不做任何自动刷新（不再用 st_autorefresh 整页轮询）——模型
    # 加载可达数分钟，每 3 秒重跑一次脚本会打断用户输入、页面闪烁。状态
    # 提示是静态的：下次任何交互（输入/点击/刷新）触发 rerun 时自然更新，
    # 转写步骤内部会自行轮询等待模型就绪，不依赖这里的刷新。
    if _backend_ready:
        asr_status = _get_core().get_asr_model_status()
        if asr_status == "loading":
            st.info("⏳ 语音识别引擎正在后台加载中…（不影响视频下载，转写时会自动等待）")
        elif asr_status == "ready":
            st.caption("✅ 语音识别引擎已就绪")
        else:
            st.caption("🔓 语音识别引擎待命（首次转写时会自动加载）")
    else:
        # bili_core 尚未 import 完成（首次访问的一次性窗口，约 10-30 秒）
        st.info("⏳ 后端服务正在初始化…（首次访问需加载识别组件，请稍候）")

    if 'video_info' in st.session_state:
        st.divider()
        info = st.session_state['video_info']
        # 下载图片到本地以避免防盗链问题并转换为base64嵌入HTML
        img_src = info['pic']
        try:
            import requests
            import base64
            pic_url = info['pic']
            if pic_url.startswith('//'):
                pic_url = 'https:' + pic_url
            
            headers = {
                'Referer': 'https://www.bilibili.com',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0'
            }
            response = requests.get(pic_url, headers=headers, timeout=10)
            if response.status_code == 200:
                img_b64 = base64.b64encode(response.content).decode("utf-8")
                mime = "image/png" if pic_url.lower().endswith(".png") else "image/jpeg"
                img_src = f"data:{mime};base64,{img_b64}"
            else:
                img_src = pic_url
        except Exception:
            img_src = info['pic']
            
        html_content = f'''
        <div class="video-info-card">
            <img src="{img_src}" style="width: 100%; border-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.08); margin-bottom: 10px;" />
            <h3>{info['title']}</h3>
            <p style="margin-bottom: 0;"><strong>UP主:</strong> {info['owner']}</p>
        </div>
        '''
        st.markdown(html_content, unsafe_allow_html=True)

    st.markdown('<div class="section-title" style="margin-top: 1.5rem;">⚡ 处理进度</div>', unsafe_allow_html=True)
    
    html_content = '<div class="progress-section">\n'
    
    if st.session_state.get('step', 0) >= 5:
        timing = st.session_state.get('timing')
        html_content += '<span class="status-badge badge-success">✅ 处理完成！</span><hr/>\n'
        if timing:
            html_content += '<h3>⏱️ 耗时统计</h3>\n'
            for k, v in timing.items():
                html_content += f'<div class="timing-item"><span>{k}</span><span>{v:.1f}秒</span></div>\n'
        elif st.session_state.get('last_completed_key') == st.session_state.get('task_key'):
            html_content += '<div class="timing-item"><span>缓存复用</span><span>无需重新处理</span></div>\n'
        else:
            html_content += '<div class="timing-item"><span>状态</span><span>已完成</span></div>\n'
    else:
        current_step = st.session_state.get('step', 0)
        if current_step > 0:
            html_content += '<span class="status-badge badge-warning">⏳ 处理中...</span>\n'
        else:
            html_content += '<span class="status-badge badge-idle">⏸️ 等待开始</span>\n'
    
    html_content += '<hr/>\n'
    
    steps_info = [
        ("📥", "获取视频信息"),
        ("💾", "下载音频"),
        ("🎵", "音频转录"),
        ("🤖", "AI 总结"),
    ]
    
    for i, (icon, name) in enumerate(steps_info):
        step_num = i + 1
        current_step = st.session_state.get('step', 0)
        cache_hit = st.session_state.get('cache_hit', False)

        # 缓存命中时，前三步直接标记为「已复用缓存」跳过状态
        if cache_hit and step_num <= 3:
            html_content += f'<div class="step-card step-skipped">{icon} {name} ⏭️ 已复用缓存</div>\n'
        elif current_step > step_num:
            html_content += f'<div class="step-card step-completed">{icon} {name} ✅</div>\n'
        elif current_step == step_num:
            html_content += f'<div class="step-card step-running">{icon} {name} ⏳</div>\n'
        else:
            html_content += f'<div class="step-card step-pending">{icon} {name}</div>\n'
            
    html_content += '</div>'
    st.markdown(html_content, unsafe_allow_html=True)

# 主内容的总结显示
st.markdown('<div class="section-title">🎬 视频总结</div>', unsafe_allow_html=True)

# 使用一个固定的容器来减少布局抖动
summary_container = st.container()

if st.session_state.get('step', 0) in [1, 2, 3] and 'current_summary' not in st.session_state:
    step_msg = {
        1: "📥 正在获取视频详细信息...",
        2: "💾 正在提取视频音频...",
        3: "🎵 正在进行语音转文字（首次使用需先加载语音引擎，请耐心等待）..."
    }
    msg = step_msg.get(st.session_state['step'], "⏳ 正在努力处理中...")
    summary_container.markdown(f'''
        <div class="summary-box" style="text-align: center; padding: 3rem 1rem;">
            <div style="font-size: 2.5rem; margin-bottom: 1rem;">⏳</div>
            <div style="font-size: 1.2rem; color: #666;">{msg}</div>
        </div>
    ''', unsafe_allow_html=True)
elif st.session_state.get('step') != 4:
    if 'final_summary' in st.session_state:
        if 'cached_summary' in st.session_state:
            st.success("🎉 已加载缓存的总结内容，无需重复处理！")
        summary_container.markdown(f'<div class="summary-box">\n\n{st.session_state["final_summary"]}\n\n</div>', unsafe_allow_html=True)
        # 仅在"刚刚完成"的那一次渲染时播放提示音，避免刷新/复用时重复响
        if st.session_state.pop('play_completion_sound', False):
            play_completion_sound()
        # 打印总结：点击置 flag → rerun → 在渲染分支 pop flag 后注入打印
        # 组件（一次性执行，避免每次 rerun 重复弹打印框）
        col_print, _ = st.columns([1, 4])
        with col_print:
            if st.button("🖨️ 打印总结", use_container_width=True):
                st.session_state['print_requested'] = True
                st.rerun()
        if st.session_state.pop('print_requested', False):
            _trigger_print_summary()
    elif 'current_summary' in st.session_state:
        summary_container.markdown(f'<div class="summary-box">\n\n{st.session_state["current_summary"]}\n\n</div>', unsafe_allow_html=True)
    else:
        summary_container.info("💡 输入视频链接并点击「开始处理」以生成总结")

if st.session_state.get('step') == 1:
    try:
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)
        
        info = _get_core().get_video_info(bvid)
        
        title = info['title']
        if len(info.get('pages', [])) > 1 and 1 <= p <= len(info['pages']):
            title = f"{title} - {info['pages'][p-1]['part']}"
        
        st.session_state['video_info'] = info
        st.session_state['title'] = title
        st.session_state['step'] = 2 # 进入第2步
        st.rerun()
    except Exception as e:
        st.error(f"❌ 获取视频信息失败: {e}")
        st.session_state['step'] = 0
        st.session_state['_submitted'] = False
        st.session_state.pop('active_task_key', None)

elif st.session_state.get('step') == 2:
    try:
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)
        title = st.session_state['title']
        
        step_start = time.time()
        title, audio_path = _get_core().download_audio(bvid, p, None)
        download_time = time.time() - step_start
        
        st.session_state['audio_path'] = audio_path
        st.session_state['title'] = title
        st.session_state['download_time'] = download_time
        st.session_state['step'] = 3
        st.rerun()
    except Exception as e:
        st.error(f"❌ 下载音频失败: {e}")
        st.session_state['step'] = 0
        st.session_state['_submitted'] = False
        st.session_state.pop('active_task_key', None)

elif st.session_state.get('step') == 3:
    try:
        audio_path = st.session_state['audio_path']

        # 若模型仍在后台加载（预热阶段），这里在脚本 run 内轮询等待：
        # 每 1 秒更新一次占位里的等待秒数，Streamlit 会把 run 中的元素
        # 更新实时推送到前端（与步骤 4 流式输出同理），"已等待 N 秒"
        # 真实滚动，页面不会看起来卡死。加超时兜底，防止模型加载异常
        # 卡住时页面无限冻结。
        wait_placeholder = summary_container.empty()
        MAX_MODEL_WAIT_SEC = 900  # 最多等 15 分钟（首次含模型下载可能很久）
        if _get_core().get_asr_model_status() == "loading":
            wait_start = time.time()
            while _get_core().get_asr_model_status() == "loading":
                elapsed = int(time.time() - wait_start)
                if elapsed >= MAX_MODEL_WAIT_SEC:
                    raise Exception(
                        f"语音识别引擎加载超时（已等待超过 {MAX_MODEL_WAIT_SEC // 60} 分钟），"
                        f"请检查后台日志或重启服务"
                    )
                wait_placeholder.markdown(
                    f'<div style="text-align:center; padding:1.5rem 0;">'
                    f'<div style="font-size:2.2rem; margin-bottom:0.8rem;">⏳</div>'
                    f'<div style="font-size:1.2rem; color:#4b5563; font-weight:600;">'
                    f'语音识别引擎正在加载中…</div>'
                    f'<div style="font-size:0.95rem; color:#9ca3af; margin-top:0.6rem;">'
                    f'已等待 {elapsed} 秒，加载完成后将自动开始转写'
                    f'（音频已下载完成，此等待不影响前面流程）'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )
                time.sleep(1)
            # 模型就绪，提示即将开始转写（转写本身可能仍需一段时间）
            wait_placeholder.markdown(
                f'<div style="text-align:center; padding:1.5rem 0;">'
                f'<div style="font-size:2.2rem; margin-bottom:0.8rem;">✅</div>'
                f'<div style="font-size:1.2rem; color:#059669; font-weight:600;">'
                f'语音识别引擎已就绪，开始转写…</div></div>',
                unsafe_allow_html=True,
            )

        # progress_callback 把"初始化引擎/加载权重/正在转写"等阶段消息实时
        # 渲染到占位里（预热未完成、由本次转写触发加载的场景下可见）
        def _transcribe_progress(msg):
            wait_placeholder.markdown(
                f'<div style="text-align:center; color:#6b7280; padding:0.5rem 0;">{msg}</div>',
                unsafe_allow_html=True,
            )

        step_start = time.time()
        text = _get_core().transcribe_audio(audio_path, _transcribe_progress)
        transcribe_time = time.time() - step_start

        wait_placeholder.empty()

        if os.path.exists(audio_path):
            os.remove(audio_path)

        st.session_state['text'] = text
        st.session_state['transcribe_time'] = transcribe_time

        # 转写完成即落盘：即使后续 AI 总结失败，下次提交同 BV 也能跳过前三步直接重试。
        _get_core().save_transcription(
            st.session_state.get('bvid'),
            st.session_state.get('title'),
            text,
            st.session_state.get('p', 1),
        )

        st.session_state['step'] = 4
        st.rerun()
    except Exception as e:
        st.error(f"❌ 音频转录失败: {e}")
        st.session_state['step'] = 0
        st.session_state['_submitted'] = False
        st.session_state.pop('active_task_key', None)

elif st.session_state.get('step') == 4:
    try:
        title = st.session_state['title']
        text = st.session_state['text']
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)

        # 缓存命中：提示用户前三步已跳过，正在基于已有转录稿重新生成总结
        if st.session_state.get('cache_hit'):
            st.info("✨ 检测到该视频已处理过，已跳过下载与转录，正在重新生成总结…")

        step_start = time.time()
        full_summary = ""
        
        # 预先创建一个空位，专门用于流式输出
        with summary_container:
            summary_placeholder = st.empty()
            # 在等待第一块内容时显示一个简单的加载状态，直接在 placeholder 中占位
            summary_placeholder.markdown('<div class="summary-box">🤖 正在组织语言并生成总结...</div>', unsafe_allow_html=True)
            
        for chunk in _get_core().summarize_content_stream(
            title, text, None,
            enable_thinking=st.session_state.get('enable_thinking', False)
        ):
            full_summary += chunk
            st.session_state['current_summary'] = full_summary
            # 流式输出时，直接更新 markdown 减少 HTML 嵌套层次带来的渲染压力
            summary_placeholder.markdown(f'<div class="summary-box">\n\n{full_summary} ▌\n\n</div>', unsafe_allow_html=True)
        
        summarize_time = time.time() - step_start
        
        # 提取 ID 用于保存
        bvid = st.session_state.get('bvid')
        
        txt_path, md_path = _get_core().save_results(bvid, title, text, full_summary, p)
        
        # 缓存命中时没有执行下载/转录，这两项不存在，用 .get 兜底为 0
        download_time = st.session_state.get('download_time', 0)
        transcribe_time = st.session_state.get('transcribe_time', 0)
        timing = {
            '音频下载': download_time,
            '音频转录': transcribe_time,
            'AI总结': summarize_time,
            '总耗时': download_time + transcribe_time + summarize_time
        }
        
        st.session_state['final_summary'] = full_summary
        st.session_state['timing'] = timing
        st.session_state['last_completed_key'] = st.session_state.get('task_key')
        st.session_state.pop('active_task_key', None)
        # 标记本次为新完成，需要在下一次渲染时播放完成提示音
        st.session_state['play_completion_sound'] = True
        st.session_state['step'] = 5
        
        st.rerun()
    except Exception as e:
        error_message = str(e)
        st.error(f"❌ {error_message}")

        st.session_state['step'] = 0
        st.session_state['_submitted'] = False
        st.session_state.pop('active_task_key', None)
