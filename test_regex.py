import re

def extract_video_id(url):
    """从 URL 中提取平台和视频 ID"""
    # B 站正则
    bili_match = re.search(r'(BV[a-zA-Z0-9]+)', url)
    if bili_match:
        bvid = bili_match.group(1)
        p_match = re.search(r'[?&]p=(\d+)', url)
        p = int(p_match.group(1)) if p_match else 1
        return "bilibili", bvid, p
        
    # YouTube 正则
    yt_match = re.search(r'(?:v=|/v/|/embed/|/shorts/|youtu\.be/|/watch\?v=)([^"&?/ ]{11})', url)
    if yt_match:
        return "youtube", yt_match.group(1), 1
        
    # 其他平台
    return "generic", url, 1

urls = [
    "https://www.bilibili.com/video/BV1Ga4y1i77D/",
    "https://www.bilibili.com/video/BV1Ga4y1i77D/?p=2",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/R4YatY6Vdrc",
    "https://twitter.com/example/status/12345"
]

for url in urls:
    platform, vid, p = extract_video_id(url)
    print(f"URL: {url}\n -> Platform: {platform}, ID: {vid}, P: {p}\n")
