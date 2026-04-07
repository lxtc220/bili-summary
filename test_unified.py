from bili_core import get_video_info_unified, extract_video_id
import json

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
        print(f"Pic URL: {info['pic'][:50]}...")
    except Exception as e:
        print(f"Error: {e}")
