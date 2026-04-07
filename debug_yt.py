import subprocess
import json
import sys

video_url = "https://www.youtube.com/watch?v=h7XaR1Vv9B0"

# 尝试不同的客户端组合
configs = [
    "youtube:player_client=ios",
    "youtube:player_client=ios,web_safari",
    "youtube:player_client=android,web_safari",
    "youtube:player_client=web_safari",
]

print(f"--- 正在针对视频 {video_url} 进行本地兼容性测试 ---")

for config in configs:
    print(f"\n[测试配置]: {config}")
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-warnings",
        "--no-check-certificate",
        "--extractor-args", config,
        video_url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            print(f"✅ 抓取信息成功! 标题: {data.get('title')}")
            
            # 测试下载 (只下载前 1 秒)
            print("正在尝试模拟下载测试...")
            dl_cmd = [
                "yt-dlp", "-x", "--audio-format", "mp3",
                "--no-check-certificate",
                "--extractor-args", config,
                "--output", "test_dl.mp3",
                "--download-sections", "*0-1",
                video_url
            ]
            dl_result = subprocess.run(dl_cmd, capture_output=True, text=True)
            if dl_result.returncode == 0:
                print("✅ 音频下载测试成功!")
                exit(0) # 找到可行方案，退出
            else:
                print(f"❌ 信息抓取成功但下载失败: {dl_result.stderr}")
        else:
            print(f"❌ 运行失败: {result.stderr}")
    except Exception as e:
        print(f"💥 异常: {e}")

print("\n!!! 未找到 100% 可行的方案，正在尝试终极备选方案...")
