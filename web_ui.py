import streamlit as st

# 1. 立即设置页面配置，减少白屏等待感
st.set_page_config(
    page_title="B站视频总结工具",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 2. 导入核心功能 (bili_core 内部已做延迟加载优化)
from bili_core import (
    extract_bvid_and_p,
    get_video_info,
    download_audio,
    transcribe_audio,
    summarize_content_stream,
    save_results
)
import time
import os
import sys
import threading
import datetime

# 自动关闭功能：如果没有活跃连接，则关闭后台
def monitor_sessions():
    """后台监控线程：如果 10 秒内没有任何网页连接，则自动关闭服务器"""
    from streamlit.runtime import get_instance
    time.sleep(10) # 启动宽限期
    
    inactive_count = 0
    while True:
        try:
            runtime = get_instance()
            # 获取当前活跃的 Session 列表
            sessions = runtime._session_mgr.list_active_sessions()
            
            if not sessions:
                inactive_count += 1
                if inactive_count >= 5: # 连续 5 次检测到无连接（约 10 秒），则关闭
                    print("检测到所有网页已关闭，正在自动退出后台进程...")
                    runtime.stop()
                    os._exit(0)
            else:
                inactive_count = 0 # 重置计数器
        except Exception:
            pass
        time.sleep(2)

# 只在第一次运行时启动监控线程
if 'monitor_started' not in st.session_state:
    st.session_state['monitor_started'] = True
    thread = threading.Thread(target=monitor_sessions, daemon=True)
    thread.start()

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');

    /* 隐藏顶部默认的 streamlit header */
    header {visibility: hidden;}
    
    /* 设定整体背景为动态渐变或高级纯色 */
    .stApp {
        background: linear-gradient(135deg, #fdfbfb 0%, #ebedee 100%);
        font-family: 'Inter', system-ui, -apple-system, sans-serif;
    }

    .block-container {
        padding-top: 1.5rem;
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
    
    if st.button("开始处理", type="primary", use_container_width=True):
        try:
            bvid, p = extract_bvid_and_p(url)
            if not bvid:
                st.error("❌ 无效的 B 站视频链接")
            else:
                st.session_state.clear()
                st.session_state['url'] = url
                st.session_state['bvid'] = bvid
                st.session_state['p'] = p
                st.session_state['step'] = 1 # 从第1步开始
                st.rerun()
        except Exception as e:
            st.error(f"❌ 处理失败: {e}")
    
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
    
    if 'timing' in st.session_state:
        timing = st.session_state['timing']
        html_content += '<span class="status-badge badge-success">✅ 处理完成！</span><hr/>\n'
        html_content += '<h3>⏱️ 耗时统计</h3>\n'
        for k, v in timing.items():
            html_content += f'<div class="timing-item"><span>{k}</span><span>{v:.1f}秒</span></div>\n'
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
        
        if current_step > step_num:
            html_content += f'<div class="step-card step-completed">{icon} {name} ✅</div>\n'
        elif current_step == step_num:
            html_content += f'<div class="step-card step-running">{icon} {name} ⏳</div>\n'
        else:
            html_content += f'<div class="step-card step-pending">{icon} {name}</div>\n'
            
    html_content += '</div>'
    st.markdown(html_content, unsafe_allow_html=True)

# 主内容的总结显示
st.markdown('<div class="section-title">📝 视频总结</div>', unsafe_allow_html=True)

# 使用一个固定的容器来减少布局抖动
summary_container = st.container()

if st.session_state.get('step', 0) in [1, 2, 3, 4] and 'current_summary' not in st.session_state:
    step_msg = {
        1: "📥 正在获取视频详细信息...",
        2: "💾 正在提取视频音频...",
        3: "🎵 正在进行语音转文字 (此步骤较慢，请耐心等待)...",
        4: "🤖 正在组织语言并生成总结..."
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
        summary_container.markdown(f'<div class="summary-box">\n\n{st.session_state["final_summary"]}\n\n</div>', unsafe_allow_html=True)
    elif 'current_summary' in st.session_state:
        summary_container.markdown(f'<div class="summary-box">\n\n{st.session_state["current_summary"]}\n\n</div>', unsafe_allow_html=True)
    else:
        summary_container.info("💡 输入视频链接并点击「开始处理」以生成总结")

if st.session_state.get('step') == 1:
    try:
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)
        
        info = get_video_info(bvid)
        
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

elif st.session_state.get('step') == 2:
    try:
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)
        title = st.session_state['title']
        
        step_start = time.time()
        title, audio_path = download_audio(bvid, p, None)
        download_time = time.time() - step_start
        
        st.session_state['audio_path'] = audio_path
        st.session_state['title'] = title
        st.session_state['download_time'] = download_time
        st.session_state['step'] = 3
        st.rerun()
    except Exception as e:
        st.error(f"❌ 下载音频失败: {e}")
        st.session_state['step'] = 0

elif st.session_state.get('step') == 3:
    try:
        audio_path = st.session_state['audio_path']
        
        step_start = time.time()
        text = transcribe_audio(audio_path, None)
        transcribe_time = time.time() - step_start
        
        if os.path.exists(audio_path):
            os.remove(audio_path)
        
        st.session_state['text'] = text
        st.session_state['transcribe_time'] = transcribe_time
        st.session_state['step'] = 4
        st.rerun()
    except Exception as e:
        st.error(f"❌ 音频转录失败: {e}")
        st.session_state['step'] = 0

elif st.session_state.get('step') == 4:
    try:
        title = st.session_state['title']
        text = st.session_state['text']
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)
        
        step_start = time.time()
        full_summary = ""
        
        # 预先创建一个空位，专门用于流式输出
        with summary_container:
            summary_placeholder = st.empty()
            
        for chunk in summarize_content_stream(title, text, None):
            full_summary += chunk
            st.session_state['current_summary'] = full_summary
            # 流式输出时，直接更新 markdown 减少 HTML 嵌套层次带来的渲染压力
            summary_placeholder.markdown(f'<div class="summary-box">\n\n{full_summary} ▌\n\n</div>', unsafe_allow_html=True)
        
        summarize_time = time.time() - step_start
        
        # 提取 ID 用于保存
        bvid = st.session_state.get('bvid')
        
        txt_path, md_path = save_results(bvid, title, text, full_summary, p)
        
        timing = {
            '音频下载': st.session_state['download_time'],
            '音频转录': st.session_state['transcribe_time'],
            'AI总结': summarize_time,
            '总耗时': st.session_state['download_time'] + st.session_state['transcribe_time'] + summarize_time
        }
        
        st.session_state['final_summary'] = full_summary
        st.session_state['timing'] = timing
        st.session_state['step'] = 5
        
        st.rerun()
    except Exception as e:
        st.error(f"❌ AI 总结失败: {e}")
        import traceback
        st.error(traceback.format_exc())
        st.session_state['step'] = 0
