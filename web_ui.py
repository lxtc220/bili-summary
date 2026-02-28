import streamlit as st
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

st.set_page_config(
    page_title="B站视频总结工具",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .block-container {
        padding-top: 1rem;
        padding-bottom: 1.5rem;
    }
    
    .main-header {
        font-size: 2rem;
        font-weight: 700;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 1rem;
    }
    
    .step-card {
        padding: 1rem;
        border-radius: 12px;
        margin-bottom: 0.75rem;
        transition: all 0.3s ease;
    }
    
    .step-pending {
        background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
        border: 2px solid #e0e0e0;
        opacity: 0.7;
    }
    
    .step-running {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border: 2px solid #667eea;
        color: white;
        animation: pulse 2s infinite;
    }
    
    .step-completed {
        background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        border: 2px solid #11998e;
        color: white;
    }
    
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.8; }
    }
    
    .summary-box {
        background: white;
        border-radius: 16px;
        padding: 1.5rem;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
    }
    
    .progress-section {
        background: white;
        border-radius: 16px;
        padding: 1.5rem;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
    }
    
    .timing-item {
        display: flex;
        justify-content: space-between;
        padding: 0.5rem 0;
        border-bottom: 1px solid #f0f0f0;
    }
    
    .timing-item:last-child {
        border-bottom: none;
        font-weight: 600;
    }
    
    .status-badge {
        display: inline-block;
        padding: 0.25rem 0.75rem;
        border-radius: 20px;
        font-size: 0.875rem;
        font-weight: 600;
    }
    
    .badge-success {
        background: #d1fae5;
        color: #065f46;
    }
    
    .badge-warning {
        background: #fef3c7;
        color: #92400e;
    }
    
    .badge-idle {
        background: #e5e7eb;
        color: #374151;
    }
    
    .video-info-card {
        background: white;
        border-radius: 12px;
        padding: 1rem;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        margin-top: 1rem;
    }
    
    .section-title {
        font-size: 1.25rem;
        font-weight: 600;
        color: #1f2937;
        margin-bottom: 1rem;
    }
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown('<h1 class="main-header">🎬 B站视频总结</h1>', unsafe_allow_html=True)
    
    url = st.text_input(
        "输入 B 站视频链接",
        value="https://www.bilibili.com/video/BV134FVzGEq6",
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
                st.session_state['step'] = 0
                st.rerun()
        except Exception as e:
            st.error(f"❌ 处理失败: {e}")
            import traceback
            st.error(traceback.format_exc())
    
    if 'video_info' in st.session_state:
        st.divider()
        info = st.session_state['video_info']
        st.markdown('<div class="video-info-card">', unsafe_allow_html=True)
        st.image(info['pic'], use_container_width=True)
        st.markdown(f"### {info['title']}")
        st.markdown(f"**UP主:** {info['owner']}")
        st.markdown('</div>', unsafe_allow_html=True)

col1, col2 = st.columns([2, 1])

with col1:
    st.markdown('<div class="section-title">📝 视频总结</div>', unsafe_allow_html=True)
    
    summary_container = st.container()
    with summary_container:
        if 'final_summary' in st.session_state:
            st.markdown('<div class="summary-box">', unsafe_allow_html=True)
            st.markdown(st.session_state['final_summary'])
            st.markdown('</div>', unsafe_allow_html=True)
        elif 'current_summary' in st.session_state:
            st.markdown('<div class="summary-box">', unsafe_allow_html=True)
            st.markdown(st.session_state['current_summary'])
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.info("👆 点击左侧栏的「开始处理」按钮开始处理视频")

with col2:
    st.markdown('<div class="section-title">⚡ 处理进度</div>', unsafe_allow_html=True)
    
    st.markdown('<div class="progress-section">', unsafe_allow_html=True)
    
    if 'timing' in st.session_state:
        timing = st.session_state['timing']
        st.markdown('<span class="status-badge badge-success">✅ 处理完成！</span>', unsafe_allow_html=True)
        st.markdown("---")
        st.markdown("### ⏱️ 耗时统计")
        for k, v in timing.items():
            st.markdown(f'<div class="timing-item"><span>{k}</span><span>{v:.1f}秒</span></div>', unsafe_allow_html=True)
    else:
        current_step = st.session_state.get('step', 0)
        if current_step > 0:
            st.markdown('<span class="status-badge badge-warning">⏳ 处理中...</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="status-badge badge-idle">⏸️ 等待开始</span>', unsafe_allow_html=True)
    
    st.markdown("---")
    
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
            st.markdown(f'<div class="step-card step-completed">{icon} {name} ✅</div>', unsafe_allow_html=True)
        elif current_step == step_num:
            st.markdown(f'<div class="step-card step-running">{icon} {name} ⏳</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="step-card step-pending">{icon} {name}</div>', unsafe_allow_html=True)
    
    st.markdown('</div>', unsafe_allow_html=True)

if st.session_state.get('step') == 0:
    try:
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)
        
        info = get_video_info(bvid)
        
        title = info['title']
        if len(info.get('pages', [])) > 1 and 1 <= p <= len(info['pages']):
            title = f"{title} - {info['pages'][p-1]['part']}"
        
        st.session_state['video_info'] = info
        st.session_state['title'] = title
        st.session_state['step'] = 1
        st.rerun()
    except Exception as e:
        st.error(f"❌ 获取视频信息失败: {e}")
        st.session_state['step'] = 0

elif st.session_state.get('step') == 1:
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
        st.session_state['step'] = 2
        st.rerun()
    except Exception as e:
        st.error(f"❌ 下载音频失败: {e}")
        st.session_state['step'] = 1

elif st.session_state.get('step') == 2:
    try:
        audio_path = st.session_state['audio_path']
        
        step_start = time.time()
        text = transcribe_audio(audio_path, None)
        transcribe_time = time.time() - step_start
        
        if os.path.exists(audio_path):
            os.remove(audio_path)
        
        st.session_state['text'] = text
        st.session_state['transcribe_time'] = transcribe_time
        st.session_state['step'] = 3
        st.rerun()
    except Exception as e:
        st.error(f"❌ 音频转录失败: {e}")
        st.session_state['step'] = 2

elif st.session_state.get('step') == 3:
    try:
        title = st.session_state['title']
        text = st.session_state['text']
        bvid = st.session_state.get('bvid')
        p = st.session_state.get('p', 1)
        
        summary_placeholder = st.empty()
        
        step_start = time.time()
        full_summary = ""
        
        for chunk in summarize_content_stream(title, text, None):
            full_summary += chunk
            st.session_state['current_summary'] = full_summary
            with summary_placeholder.container():
                st.markdown('<div class="summary-box">', unsafe_allow_html=True)
                st.markdown(full_summary)
                st.markdown('</div>', unsafe_allow_html=True)
        
        summarize_time = time.time() - step_start
        
        txt_path, md_path = save_results(bvid, title, text, full_summary, p)
        
        timing = {
            '音频下载': st.session_state['download_time'],
            '音频转录': st.session_state['transcribe_time'],
            'AI总结': summarize_time,
            '总耗时': st.session_state['download_time'] + st.session_state['transcribe_time'] + summarize_time
        }
        
        st.session_state['final_summary'] = full_summary
        st.session_state['timing'] = timing
        st.session_state['step'] = 4
        
        st.rerun()
    except Exception as e:
        st.error(f"❌ AI 总结失败: {e}")
        import traceback
        st.error(traceback.format_exc())
        st.session_state['step'] = 3
