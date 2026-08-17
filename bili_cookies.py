"""B站登录 cookie 获取（独立小模块，只依赖 yt-dlp）。

为什么单独成模块：web_ui 启动时要在打开浏览器**之前**把 cookie 抢先
提取好（Edge/Chrome 运行中会锁 cookie 库，提取必败），而 bili_core
顶层 import funasr 等重型依赖（约 30 秒），来不及。本模块顶层只有
标准库，yt-dlp 懒加载，启动路径上足够快。

网页本身读不了跨域 cookie（同源策略），这里的提取走 yt-dlp 的
浏览器 cookie 数据库读取（与 --cookies-from-browser 同一条代码路径），
因此要求提取时刻浏览器完全退出（含"启动增强"后台常驻进程）。
"""

import os
import sys
import threading

_warmed = None  # 提取成功：{name: value}；失败/未配置：{}；未尝试：None
_warm_lock = threading.Lock()

_WANTED = ("SESSDATA", "bili_jct", "buvid3", "DedeUserID")


def warm_from_browser(timeout_sec=10):
    """提取浏览器中的B站登录 cookie，结果进程内缓存（幂等）。

    返回含 SESSDATA 的 dict；未配置 BILIBILI_COOKIES_FROM_BROWSER、
    浏览器未登录、提取超时/失败都返回 None。绝不上抛异常。
    """
    global _warmed
    browser = os.environ.get("BILIBILI_COOKIES_FROM_BROWSER", "").strip()
    if not browser:
        _warmed = {}
        return None

    with _warm_lock:
        if _warmed:
            return _warmed if "SESSDATA" in _warmed else None

        done = threading.Event()
        holder = {}

        def worker():
            try:
                from yt_dlp import YoutubeDL

                with YoutubeDL({"cookiesfrombrowser": (browser,),
                                "quiet": True}) as ydl:
                    jar = ydl.cookiejar
                values = {}
                for cookie in jar:
                    domain = cookie.domain or ""
                    if domain.endswith("bilibili.com") and cookie.value \
                            and cookie.name in _WANTED \
                            and cookie.name not in values:
                        values[cookie.name] = cookie.value
                holder["values"] = values
            except Exception as e:
                print(f"浏览器 {browser} cookie 提取失败: {e}", file=sys.stderr)
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        done.wait(timeout_sec)
        values = holder.get("values") or {}
        if "SESSDATA" in values:
            _warmed = values
            print(f"已从浏览器 {browser} 读取B站登录态（AI字幕可用）", flush=True)
            return _warmed
        if _warmed is None:
            _warmed = {}
        print(f"浏览器 {browser} 未读到B站登录态（字幕回退游客态/本地转录）",
              flush=True)
        return None


def get_warmed():
    """返回已提取成功的 cookie dict；没有则 None（不触发新的提取）。"""
    return _warmed if (_warmed and "SESSDATA" in _warmed) else None
