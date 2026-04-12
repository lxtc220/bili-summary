import os
import sys
from bili_core import (
    extract_bvid_and_p, 
    download_audio, 
    transcribe_audio, 
    summarize_content, 
    save_results,
    VIDEO_URL
)

def main():
    """主函数"""
    print("\n" + "="*50)
    print("B站视频内容总结脚本")
    print("="*50)
    
https://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38bhttps://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38b    try:
        url = "https://www.bilibili.com/video/BV1b1DjBWEkN/?spm_id_from=333.1391.0.0&vd_source=6cec98d87e21778c3c0afc1d666bf38b"
        print(f"处理的URL: {url}")
        
        bvid, p = extract_bvid_and_p(url)
        if not bvid:
            print("无效的B站视频URL，无法提取BV号")
            sys.exit(1)
            
        print(f"提取到的BV号: {bvid}, 分集号: {p}")
        
        # 1. 下载音频
        print("\n3.1 下载音频")
        title, audio_path = download_audio(bvid, p, lambda msg: print(f"   {msg}"))

        # 2. 转写音频为文字
        print("\n3.3 音频转文字")
        text = transcribe_audio(audio_path, lambda msg: print(f"   {msg}"))

        # 3. 总结
        print("\n3.4 内容总结")
        summary = summarize_content(title, text, lambda msg: print(f"   {msg}"))

        # 4. 保存结果
        print("\n3.5 保存结果")
        txt_path, md_path = save_results(bvid, title, text, summary, p)

        print("\n" + "="*50)
        print("任务完成")
        print("="*50)
        print(f"转录结果已保存到: {txt_path}")
        print(f"总结结果已保存到: {md_path}")
    except Exception as e:
        print(f"\n发生异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
