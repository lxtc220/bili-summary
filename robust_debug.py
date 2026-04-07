import subprocess
import os

video_url = "https://www.youtube.com/watch?v=h7XaR1Vv9B0"
test_file = "test_robust.mp3"

# 尝试所有已知的解决 YouTube 403 的签名组合
test_configs = [
    ["--extractor-args", "youtube:player_client=ios"],
    ["--extractor-args", "youtube:player_client=android_embedded"],
    ["--extractor-args", "youtube:player_client=web_embedded"],
    ["--extractor-args", "youtube:player_client=mweb"],
    ["--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"],
    ["--force-ipv4"]
]

print(f"--- 核心下载能力深度测试 ---")

for i, config in enumerate(test_configs):
    print(f"\n[尝试方案 {i+1}]: {' '.join(config)}")
    if os.path.exists(test_file): os.remove(test_file)
    
    cmd = [
        "yt-dlp",
        "-x", "--audio-format", "mp3",
        "--no-check-certificate",
        "--no-warnings",
        "--download-sections", "*0-1",
        "-o", test_file
    ] + config + [video_url]
    
    try:
        # 使用 check=True 触发异常
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and os.path.exists(test_file):
            print(f"🎊 方案 {i+1} 成功下载音频!")
            print(f"最终建议参数: {' '.join(config)}")
            # 记录成功参数到成功日志
            with open("yt_success.txt", "w") as f: f.write(" ".join(config))
            exit(0)
        else:
            print(f"❌ 失败: {result.stderr.splitlines()[-1] if result.stderr.splitlines() else '未知错误'}")
    except Exception as e:
        print(f"💥 异常: {e}")

print("\n!!! 遗憾：当前 IP 或环境已被 YouTube 深度限制。尝试最后的 '原生提取' 方案...")
