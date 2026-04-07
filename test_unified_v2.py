from bili_core import get_video_info_unified, extract_video_id
import json
import sys

# 设置编码以防止 Windows 终端乱码
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

urls = [
    "https://www.bilibili.com/video/BV1Ga4y1i77D/",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
]

for url in urls:
    print(f"\n--- Testing URL: {url} ---")
    platform, vid, p = extract_video_id(url)
    print(f"Platform: {platform}, ID: {vid}, P: {p}")
    try:
        info = get_video_info_unified(url)
        print(f"Title: {info['title']}")
        print(f"Owner: {info['owner']}")
        # 验证图片链接是否存在
        if info['pic']:
            print(f"Pic URL OK (starts with {info['pic'][:20]}...)")
        else:
            print("Pic URL Missing!")
    except Exception as e:
        print(f"Error: {e}")
