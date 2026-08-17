import os
import sys
import re
import subprocess
import threading
import time
from pathlib import Path

# 配置信息 - 使用本地 ffmpeg。
# 目录版 EXE 的业务根目录由 GUI 通过 BILI_SUMMARY_ROOT 传给后台引擎，
# 避免把模型缓存、.env 和结果文件写入 PyInstaller 的 _internal 目录。
_configured_root = os.environ.get("BILI_SUMMARY_ROOT")
if _configured_root:
    PROJECT_ROOT = Path(_configured_root).resolve()
elif getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent

ffmpeg_path = str(PROJECT_ROOT / "ffmpeg")
if ffmpeg_path not in os.environ["PATH"]:
    os.environ["PATH"] = f"{ffmpeg_path};{os.environ['PATH']}"

# 重要：funasr 必须在 bilibili_api / modelscope 之前 import。
# 在 Windows 上，若 bilibili_api(curl_cffi) 和 modelscope 先加载，
# 会改变某些共享 native DLL 的状态，导致后续 funasr 内部 torch.jit
# 编译 bicif_paraformer/cif_predictor.py 时触发 access violation（段错误）。
# 因此 funasr 在本模块顶层最先 import；bilibili_api / modelscope / openai
# 这些较重且启动非必需的依赖改为在各自函数内懒加载（首次调用时 funasr
# 必已就位，顺序约束不变），避免 web_ui / api 入口 import 本模块时被拖慢。
try:
    import funasr  # noqa: F401
except Exception:
    # 模块缺失等情况下不阻塞 bili_core 其他功能（如 LLM 总结）
    pass

import json
from dotenv import load_dotenv

# 加载环境变量
dotenv_path = PROJECT_ROOT / ".env"
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# AI 模型配置 (支持所有兼容 OpenAI 接口的服务商)
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.avemujica.moe/v1")
MODEL_ID = os.environ.get("MODEL_ID", "gpt-5.6-sol")

# B站配置
DEFAULT_BILI_USER_AGENT = os.environ.get(
    "BILIBILI_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
DEFAULT_BILI_REFERER = os.environ.get("BILIBILI_REFERER", "https://www.bilibili.com/")
DEFAULT_BILI_ORIGIN = os.environ.get("BILIBILI_ORIGIN", "https://www.bilibili.com")
_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]p=(\d+)")

# 验证 API 密钥是否存在
if not LLM_API_KEY:
    print("警告: 未检测到 LLM_API_KEY 环境变量，AI 总结功能将不可用。", file=sys.stderr)


class LLMServiceError(Exception):
    """AI 服务调用失败，message 可直接展示给用户。"""


def _extract_error_code(error):
    """兼容 OpenAI SDK 与各类 OpenAI-compatible 服务商的错误结构。"""
    for attr in ("code", "type"):
        value = getattr(error, attr, None)
        if value:
            return str(value)

    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested_error = body.get("error")
        if isinstance(nested_error, dict):
            return str(nested_error.get("code") or nested_error.get("type") or "")

    return ""


def _format_llm_error(error: Exception) -> str:
    """格式化 LLM 错误信息，提供用户友好的提示。"""
    import httpx

    status_code = None
    raw_message = str(error)

    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
    if hasattr(error, "status_code"):
        status_code = error.status_code
    if hasattr(error, "code"):
        status_code = error.code

    if status_code == 401:
        return (
            "AI 总结失败：API Key 无效或已过期。请检查 .env 文件或环境变量中的 LLM_API_KEY 配置。"
        )

    if status_code == 404:
        return (
            "AI 总结失败：模型不存在或当前账号无权调用该模型。"
            f"请检查 .env 中的 MODEL_ID。当前 MODEL_ID={MODEL_ID}。"
        )

    suffix = f"（服务商返回：{raw_message}）" if raw_message else ""
    return f"AI 总结失败：调用 AI 服务时出错{suffix}"


def extract_bvid_and_p(url):
    """从URL中提取BV号和分集号"""
    p = 1

    if not url:
        return None, p

    page_match = _PAGE_RE.search(url)
    if page_match:
        try:
            p = int(page_match.group(1))
        except ValueError:
            p = 1

    bvid_match = _BVID_RE.search(url)
    if bvid_match:
        bvid = bvid_match.group(1)
        return "BV" + bvid[2:], p

    return None, p


def _resolve_bili_video_url(source_url, bvid, page=1):
    """尽量保留用户输入的原始链接；否则退回到标准 BV 页面链接。"""
    if source_url and source_url.startswith(("http://", "https://")):
        return source_url

    video_url = f"https://www.bilibili.com/video/{bvid}"
    if page > 1:
        video_url += f"?p={page}"
    return video_url


def _extend_yt_dlp_command(cmd):
    """为 yt-dlp 补充更像浏览器的请求头和可选 cookies。

    B 站 CDN 经常在下载途中断开连接（"N bytes read, M more expected"），
    这里通过分块下载（--http-chunk-size）让断点可续传，并提高重试次数，
    避免整段音频因单次连接中断而失败。
    """
    cmd.extend([
        "--no-check-certificate",
        "--no-update",
        "--retries", "10",
        "--fragment-retries", "10",
        "--extractor-retries", "10",
        # 分块下载：B 站 CDN 不稳定时只重传当前块，而不是整段重来
        "--http-chunk-size", "10485760",
        # 指定 buffer 大小，缓解长连接被服务端提前关闭的问题
        "--buffer-size", "16384",
        "--user-agent", DEFAULT_BILI_USER_AGENT,
        "--add-header", f"Referer: {DEFAULT_BILI_REFERER}",
        "--add-header", f"Origin: {DEFAULT_BILI_ORIGIN}",
    ])

    # 环境变量指定的文件 > 扫码登录落盘的默认文件（未登录则两者皆无）
    cookie_file = os.environ.get("BILIBILI_COOKIE_FILE")
    if not (cookie_file and os.path.exists(cookie_file)) \
            and os.path.exists(_default_bili_cookie_file()):
        cookie_file = _default_bili_cookie_file()
    cookie_from_browser = os.environ.get("BILIBILI_COOKIES_FROM_BROWSER")

    if cookie_file:
        cmd.extend(["--cookies", cookie_file])
    elif cookie_from_browser and _load_bili_credential() is not None:
        # 浏览器 cookie 提取可用才加该参数：Edge/Chrome 运行中会锁住
        # cookie 库导致提取失败，直接传给 yt-dlp 会让整个下载报错；
        # 此时退回无 cookie 模式（与未配置时行为一致）
        cmd.extend(["--cookies-from-browser", cookie_from_browser])

    return cmd


def download_paraformer_model(progress_callback=None):
    """下载Paraformer模型"""
    if progress_callback: progress_callback("正在下载Paraformer模型...")

    model_cache_dir = str(PROJECT_ROOT / "model_cache" / "models" / "iic")
    os.makedirs(model_cache_dir, exist_ok=True)

    model_id = "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    target_dir = os.path.join(model_cache_dir, "paraformer-zh")

    if os.path.exists(target_dir):
        return target_dir

    try:
        # modelscope 懒加载：仅在需要下载模型时才 import（此时 funasr 已加载）
        from modelscope.hub.snapshot_download import snapshot_download
        model_dir = snapshot_download(
            model_id,
            cache_dir=model_cache_dir,
            revision="master"
        )

        import shutil
        shutil.copytree(model_dir, target_dir)
        return target_dir
    except Exception as e:
        raise Exception(f"下载模型失败: {e}")


def download_vad_model(progress_callback=None):
    """下载VAD模型（fsmn-vad，用于检测语音段并据此断句）"""
    if progress_callback: progress_callback("正在下载VAD模型...")

    model_cache_dir = str(PROJECT_ROOT / "model_cache" / "models" / "iic")
    os.makedirs(model_cache_dir, exist_ok=True)

    model_id = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
    target_dir = os.path.join(model_cache_dir, "fsmn-vad")

    if os.path.exists(target_dir):
        return target_dir

    try:
        # modelscope 懒加载：仅在需要下载模型时才 import（此时 funasr 已加载）
        from modelscope.hub.snapshot_download import snapshot_download
        model_dir = snapshot_download(
            model_id,
            cache_dir=model_cache_dir,
            revision="master"
        )

        import shutil
        shutil.copytree(model_dir, target_dir)
        return target_dir
    except Exception as e:
        raise Exception(f"下载VAD模型失败: {e}")


def download_sensevoice_model(progress_callback=None):
    """
    下载SenseVoiceSmall模型。

    注意：必须放到纯英文路径下。sentencepiece 0.2.1+ 在 Windows 加载
    中文路径下的 bpe.model 会段错误（access violation），所以这里
    显式指定英文子目录名 'sense-voice'，而不是用 ModelScope 默认的
    含中文用户名的缓存目录。
    """
    if progress_callback: progress_callback("正在下载SenseVoice模型...")

    model_cache_dir = str(PROJECT_ROOT / "model_cache" / "models" / "iic")
    os.makedirs(model_cache_dir, exist_ok=True)

    model_id = "iic/SenseVoiceSmall"
    target_dir = os.path.join(model_cache_dir, "sense-voice")

    if os.path.exists(target_dir):
        return target_dir

    try:
        # modelscope 懒加载：仅在需要下载模型时才 import（此时 funasr 已加载）
        from modelscope.hub.snapshot_download import snapshot_download
        model_dir = snapshot_download(
            model_id,
            cache_dir=model_cache_dir,
            revision="master"
        )

        import shutil
        shutil.copytree(model_dir, target_dir)
        return target_dir
    except Exception as e:
        raise Exception(f"下载SenseVoice模型失败: {e}")


# --- ASR 模型单例与异步预加载 ---
# 模型实例放模块级全局，后台引擎进程内只保留一份；不能放进界面层状态，
# 否则每个窗口或会话都可能复制一份，显存会爆。
# 进程存活期间常驻；触发销毁的唯一条件是后台引擎进程退出
# （停止程序.bat / 手动重启）。
_asr_model_instance = None
_asr_model_lock = threading.Lock()


def preload_asr_model(progress_callback=None):
    """
    加载 SenseVoice + fsmn-vad 组合模型到全局单例（双检锁，幂等）。

    可被两类调用方触发：
      1. desktop_engine.py 启动时触发的预热流程；
      2. transcribe_audio() 里，若单例还没就绪则阻塞等待加载完成。
    无论被并发触发多少次，模型只会加载一次。
    """
    global _asr_model_instance

    # 第一次无锁快检：已就绪直接返回，避免每次转写都抢锁
    if _asr_model_instance is not None:
        return _asr_model_instance

    with _asr_model_lock:
        # 抢到锁后再查一次，防止两个线程同时通过第一次检查
        if _asr_model_instance is not None:
            return _asr_model_instance

        if progress_callback:
            progress_callback("正在初始化 ASR 引擎 (SenseVoice + fsmn-vad)...")

        try:
            import torch
            os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'

            from funasr import AutoModel
            import gc

            torch.cuda.empty_cache()
            gc.collect()

            device = "cuda" if torch.cuda.is_available() else "cpu"
            if progress_callback:
                progress_callback(f"使用设备: {device}")

            # 确保模型文件就位
            local_asr_path = str(
                PROJECT_ROOT / "model_cache" / "models" / "iic" / "sense-voice"
            )
            if not os.path.exists(local_asr_path):
                download_sensevoice_model(progress_callback)

            local_vad_path = str(
                PROJECT_ROOT / "model_cache" / "models" / "iic" / "fsmn-vad"
            )
            if not os.path.exists(local_vad_path):
                download_vad_model(progress_callback)

            # 加载 ASR + 自动挂载 VAD（funasr 内部完成 VAD→切段→ASR→拼接）
            if progress_callback:
                progress_callback("正在加载模型权重...")
            model = AutoModel(
                model=local_asr_path,
                vad_model=local_vad_path,
                vad_kwargs={"max_single_segment_time": 30000},
                trust_remote_code=False,
                device=device,
                disable_update=True,
            )

            _asr_model_instance = model
            if progress_callback:
                progress_callback(f"ASR 引擎已就绪 (设备: {device})")
            return _asr_model_instance

        except Exception as e:
            if progress_callback:
                progress_callback(f"ASR 引擎加载失败: {e}")
            raise


def get_asr_model_status():
    """
    返回 ASR 引擎当前状态（供后台引擎或界面展示进度用，非阻塞）。
      "ready"   - 已就绪，可立即转写
      "loading" - 正在后台加载中
      "idle"    - 尚未触发加载
    """
    if _asr_model_instance is not None:
        return "ready"
    # 锁被占用 = 正在加载；锁空闲 = 还没开始
    if _asr_model_lock.locked():
        return "loading"
    return "idle"


def get_video_info(bvid):
    """获取视频详细信息"""
    try:
        # bilibili_api 懒加载（此时 funasr 已由模块级导入先加载，保持
        # "funasr 先于 bilibili_api/modelscope"的顺序，规避 Windows 段错误）。
        # bilibili_api 默认用 curl_cffi 作为 HTTP 客户端，但本机上 curl_cffi 的
        # native libcurl 加载证书会报 "error setting certificate verify locations"
        # （curl: 77），导致所有调用 B站 API 的操作全部失败。改用 httpx 客户端
        # 绕开 curl_cffi，证书验证走 Python 标准库，问题消失。
        try:
            from bilibili_api.utils.network import select_client
            select_client("httpx")
        except Exception:
            # 老版本 bilibili_api 无 select_client，保持默认（curl_cffi）
            pass
        from bilibili_api import video, sync

        v = video.Video(bvid=bvid)
        info = sync(v.get_info())
        return {
            "title": info['title'],
            "desc": info['desc'],
            "pic": info['pic'],
            "owner": info['owner']['name'],
            "owner_face": info['owner']['face'],
            "duration": info['duration'],
            "pubdate": info['pubdate'],
            "stat": info['stat'],
            "pages": info.get('pages', [])
        }
    except Exception as e:
        raise Exception(f"获取视频信息失败: {e}")


# 模块级缓存：B站登录凭证（从 BILIBILI_COOKIE_FILE 解析；无则游客态）
_bili_credential = None
_bili_credential_loaded = False


def _credential_from_browser():
    """用 yt-dlp 的浏览器 cookie 提取能力拿B站登录态。

    优先使用 web_ui 启动时 bili_cookies 的预热缓存（预热抢在启动脚本
    自动打开浏览器之前，那时 cookie 库未被锁）；未预热时现场提取
    （浏览器运行中大概率失败）。失败返回 None，绝不抛异常、绝不打印
    cookie 值。
    """
    try:
        from bili_cookies import warm_from_browser
        values = warm_from_browser()
        if not values:
            return None
        from bilibili_api import Credential
        return Credential(
            sessdata=values["SESSDATA"],
            bili_jct=values.get("bili_jct"),
            buvid3=values.get("buvid3"),
            dedeuserid=values.get("DedeUserID"),
        )
    except Exception as e:
        print(f"从浏览器提取B站 cookie 失败: {e}", file=sys.stderr)
        return None


def _load_bili_credential():
    """加载B站登录态，优先级：BILIBILI_COOKIE_FILE > BILIBILI_COOKIES_FROM_BROWSER > 游客。

    AI 字幕接口通常需要登录才返回字幕列表；拿不到登录态以游客态尝试
    （查不到 AI 字幕，调用方需做好回退）。
    """
    global _bili_credential, _bili_credential_loaded
    if _bili_credential_loaded:
        return _bili_credential
    _bili_credential_loaded = True

    # 环境变量指定的文件 > 扫码登录落盘的默认文件 bili_cookies.txt
    cookie_file = os.environ.get("BILIBILI_COOKIE_FILE")
    if not (cookie_file and os.path.exists(cookie_file)):
        default_file = _default_bili_cookie_file()
        if os.path.exists(default_file):
            cookie_file = default_file
    if cookie_file and os.path.exists(cookie_file):
        try:
            wanted = {}
            with open(cookie_file, "r", encoding="utf-8") as f:
                for line in f:
                    # netscape 格式：域\t包含\t路径\t安全\t过期\t名称\t值
                    # （注释行/失效行字段数不是 7，自然被跳过；#HttpOnly_ 前缀不影响）
                    parts = line.strip().split("\t")
                    if len(parts) != 7:
                        continue
                    name, value = parts[5], parts[6]
                    if name in ("SESSDATA", "bili_jct", "buvid3", "DedeUserID") \
                            and name not in wanted:
                        wanted[name] = value
            if "SESSDATA" in wanted:
                from bilibili_api import Credential
                _bili_credential = Credential(
                    sessdata=wanted["SESSDATA"],
                    bili_jct=wanted.get("bili_jct"),
                    buvid3=wanted.get("buvid3"),
                    dedeuserid=wanted.get("DedeUserID"),
                )
        except Exception as e:
            print(f"解析 BILIBILI_COOKIE_FILE 失败: {e}", file=sys.stderr)

    if _bili_credential is None:
        browser = os.environ.get("BILIBILI_COOKIES_FROM_BROWSER")
        if browser:
            _bili_credential = _credential_from_browser()
    return _bili_credential


def _download_subtitle_body(url):
    """下载字幕 JSON 并拼接为纯文本；失败返回 None。"""
    import requests

    response = requests.get(
        url,
        headers={
            "Referer": DEFAULT_BILI_REFERER,
            "User-Agent": DEFAULT_BILI_USER_AGENT,
        },
        timeout=15,
    )
    if response.status_code != 200:
        return None
    body = (response.json() or {}).get("body") or []
    text = " ".join(
        line.get("content", "").strip() for line in body if line.get("content")
    ).strip()
    return text or None


def _fetch_subtitle_tracks_guest(aid, cid):
    """游客态走老版 player/v2 接口查询字幕列表（新版 wbi 接口强制登录）。

    AI 字幕登录后才可见，游客态最多拿到 UP 主上传的 CC 字幕。
    """
    import requests

    resp = requests.get(
        "https://api.bilibili.com/x/player/v2",
        params={"aid": aid, "cid": cid},
        headers={
            "Referer": DEFAULT_BILI_REFERER,
            "User-Agent": DEFAULT_BILI_USER_AGENT,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return []
    data = resp.json() or {}
    if data.get("code") != 0:
        return []
    return (((data.get("data") or {}).get("subtitle") or {})
            .get("subtitles")) or []


def fetch_subtitle_text(bvid, p=1, progress_callback=None):
    """尝试读取B站字幕文字稿：UP主中文字幕优先，其次B站AI字幕。

    拿不到（无字幕 / 未登录看不到AI字幕 / 网络失败）一律返回 None，
    由调用方回退到"下载音频 + 本地转录"流程，绝不抛异常。
    """
    try:
        # 与 get_video_info 相同的懒加载模式（funasr 已在模块顶部先加载）
        try:
            from bilibili_api.utils.network import select_client
            select_client("httpx")
        except Exception:
            pass
        from bilibili_api import video, sync

        credential = _load_bili_credential()
        v = video.Video(bvid=bvid, credential=credential)
        info = sync(v.get_info()) or {}
        pages = info.get("pages", [])
        if not (1 <= p <= len(pages)):
            return None
        cid = pages[p - 1]["cid"]

        if progress_callback:
            progress_callback("正在查询B站字幕...")
        if credential is not None:
            # 登录态：官方 player 接口，可见 AI 字幕与 UP 主字幕
            tracks = ((sync(v.get_subtitle(cid=cid)) or {})
                      .get("subtitles", []) or [])
        else:
            # 游客态：老版接口碰运气（最多拿到 UP 主上传的 CC 字幕）
            tracks = _fetch_subtitle_tracks_guest(info.get("aid"), cid)

        def track_rank(track):
            lan = str(track.get("lan", ""))
            if lan == "zh-CN":
                return 0            # UP主上传的中文字幕
            if lan.startswith("ai-zh") or track.get("ai_type") == 1:
                return 1            # B站AI中文字幕
            if "zh" in lan:
                return 2            # 其他中文轨道
            return 3

        tracks.sort(key=track_rank)
        for track in tracks:
            url = str(track.get("subtitle_url") or "")
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            text = _download_subtitle_body(url)
            if text:
                return text
        return None
    except Exception as e:
        print(f"获取B站字幕失败（回退本地转录）: {e}", file=sys.stderr)
        return None


def _default_bili_cookie_file():
    """扫码登录写入的默认 cookie 文件（项目根目录 bili_cookies.txt）。

    不依赖 .env 配置即可生效：_load_bili_credential 与 yt-dlp 下载
    都会在环境变量未配置时回退到这个文件。
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bili_cookies.txt")


def generate_bili_login_qrcode():
    """生成B站扫码登录二维码，返回 (qrcode_key, 扫码URL)。"""
    import requests

    resp = requests.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        headers={"User-Agent": DEFAULT_BILI_USER_AGENT,
                 "Referer": "https://www.bilibili.com/"},
        timeout=10)
    data = resp.json() or {}
    if data.get("code") != 0:
        raise Exception(f"获取登录二维码失败: {data.get('message', '未知错误')}")
    d = data.get("data") or {}
    if not d.get("qrcode_key") or not d.get("url"):
        raise Exception("获取登录二维码失败：返回数据不完整")
    return d["qrcode_key"], d["url"]


def _save_bili_cookies_file(values):
    """把登录凭证写为 netscape cookie 文件（凭证加载与 yt-dlp 共用）。"""
    path = _default_bili_cookie_file()
    lines = ["# Netscape HTTP Cookie File（B站视频总结工具扫码登录生成）"]
    for name in ("SESSDATA", "bili_jct", "buvid3", "DedeUserID"):
        if values.get(name):
            lines.append(f".bilibili.com\tTRUE\t/\tTRUE\t0\t{name}\t{values[name]}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def poll_bili_login_qrcode(qrcode_key):
    """轮询扫码状态；确认成功即落盘 cookie 并刷新进程内登录态。

    返回 {"status": waiting/scanned/expired/confirmed, "path": 落盘路径}。
    网络异常直接上抛，由调用方给出中文提示。
    """
    import requests

    resp = requests.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
        params={"qrcode_key": qrcode_key},
        headers={"User-Agent": DEFAULT_BILI_USER_AGENT,
                 "Referer": "https://www.bilibili.com/"},
        timeout=10)
    body = resp.json() or {}
    inner = (body.get("data") or {}).get("code", -1)
    if inner != 0:
        return {"status": {86101: "waiting", 86090: "scanned",
                           86038: "expired"}.get(inner, "unknown")}

    # 确认成功：凭证在响应 cookie 里，部分字段也可能只出现在回跳 URL 中
    values = {}
    for cookie in resp.cookies:
        if cookie.name in ("SESSDATA", "bili_jct", "buvid3", "DedeUserID") \
                and cookie.value:
            values.setdefault(cookie.name, cookie.value)
    from urllib.parse import urlparse, parse_qs
    for key, vals in parse_qs(urlparse(
            (body.get("data") or {}).get("url", "")).query).items():
        if key in ("SESSDATA", "bili_jct", "buvid3", "DedeUserID") and vals:
            values.setdefault(key, vals[0])
    if "SESSDATA" not in values:
        raise Exception("登录成功但未解析到登录凭证，请刷新二维码重试")

    path = _save_bili_cookies_file(values)
    # 进程内立即生效：无需重启，当前服务的 AI 字幕 / 带 cookie 下载
    # 马上可用（_load_bili_credential 之后直接返回该实例）
    global _bili_credential
    from bilibili_api import Credential
    _bili_credential = Credential(
        sessdata=values["SESSDATA"],
        bili_jct=values.get("bili_jct"),
        buvid3=values.get("buvid3"),
        dedeuserid=values.get("DedeUserID"),
    )
    return {"status": "confirmed", "path": path}


def bili_login_ready():
    """B站登录态是否可用（决定 AI 字幕与带 cookie 下载是否启用）。"""
    return _load_bili_credential() is not None


# 模块级：下载互斥锁，按任务键 {bvid}_p{page} 区分。
# Streamlit 的 rerun（重复点击按钮 / 刷新页面 / autorefresh）可能让
# download_audio 被并发调用，多个 yt-dlp 写同一输出文件会互相破坏
# （.part 互踩），导致下载永远无法完成、页面卡死。锁保证同一任务
# 同时只有一个下载在跑；后到的在等锁期间，先到的已下载完成，文件
# 已存在 → 直接跳过下载返回。
_download_locks = {}
_download_locks_guard = threading.Lock()


def _get_download_lock(bvid, page):
    """按任务键获取（必要时创建）下载互斥锁。"""
    key = f"{bvid}_p{page}"
    with _download_locks_guard:
        lock = _download_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _download_locks[key] = lock
        return lock


def download_audio(bvid, page=1, progress_callback=None):
    """下载B站视频的音频（同一任务互斥，防并发写同一文件卡死）"""
    lock = _get_download_lock(bvid, page)
    with lock:
        return _do_download_audio(bvid, page, progress_callback)


def _do_download_audio(bvid, page, progress_callback):
    """download_audio 的实际实现（调用方需已持有该任务的互斥锁）。"""
    if progress_callback: progress_callback(f"正在下载视频音频 (BV: {bvid}, P: {page})...")

    os.makedirs("intermediate_files", exist_ok=True)

    try:
        info = get_video_info(bvid)
        title = info['title']

        if len(info['pages']) > 1:
            audio_path = os.path.join("intermediate_files", f"{bvid}_p{page}.mp3")
            cmd = ["yt-dlp", "--playlist-items", str(page), "-x", "--audio-format", "mp3", "-o", audio_path, f"https://www.bilibili.com/video/{bvid}"]
            if 0 < page <= len(info['pages']):
                title = f"{title} - {info['pages'][page-1]['part']}"
        else:
            audio_path = os.path.join("intermediate_files", f"{bvid}.mp3")
            cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", audio_path, f"https://www.bilibili.com/video/{bvid}"]

        cmd = _extend_yt_dlp_command(cmd)

        if not os.path.exists(audio_path):
            # 在 Windows 上隐藏子进程黑框
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0

            # B 站 CDN 经常不稳定，整条命令也重试几次；yt-dlp 自带 --continue 可断点续传
            last_err = None
            for attempt in range(1, 4):
                result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
                if result.returncode == 0:
                    break
                last_err = result.stderr
                if progress_callback:
                    progress_callback(f"下载失败，正在重试 ({attempt}/3)...")
                time.sleep(2 * attempt)
            else:
                raise Exception(f"音频下载失败: {last_err}")

        return title, audio_path
    except Exception as e:
        raise Exception(f"下载音频异常: {e}")


def transcribe_audio(audio_path, progress_callback=None):
    """
    使用 SenseVoiceSmall 模型将音频转换为文字（funasr 自动流水线版）。

    通过 AutoModel(vad_model=...) 让 funasr 内部自动完成
    "VAD 检测 → 按静音切段 → 逐段 ASR → 拼接" 的完整流水线，
    无需在 Python 侧手动切片。max_single_segment_time 限制单段最长
    30 秒，长音频会自动按 VAD 边界切短，避免显存溢出。

    模型实例由 preload_asr_model() 全局单例管理：首次调用时若单例尚未
    就绪会阻塞等待加载，之后就绪状态下直接复用（不再每次重新加载、
    也不再 del）。模型常驻显存，直到 Streamlit server 进程退出。

    SenseVoiceSmall 自带标点、语种/情感/事件标签（<|zh|><|NEUTRAL|><|BGM|>等）
    以及 ITN（数字归一化），因此无需额外 punc 模型即可得到带标点的转写结果，
    也无需再像 Paraformer 那样手动补句号。标签按设计保留，不做清洗。
    """
    try:
        # 复用全局单例；若后台预加载尚未完成，这里会阻塞等待
        model = preload_asr_model(progress_callback)

        # 整段音频直接交给 funasr，language=auto 自动识别语种，use_itn=True 开启 ITN
        if progress_callback:
            progress_callback("正在转写...")
        res = model.generate(
            input=audio_path,
            language="auto",
            use_itn=True,
        )

        if isinstance(res, list) and len(res) > 0:
            return res[0].get("text", "")
        return ""

    except Exception as e:
        raise Exception(f"音频转文字失败: {e}")


def _build_system_prompt(word_limit):
    """构建总结系统提示词；word_limit 为正整数时附加篇幅要求，None/0 不限制。"""
    length_req = f"，总结篇幅控制在{int(word_limit)}字以内" if word_limit else ""
    return (
        "你是一个专业的视频内容总结助手，请根据提供的视频标题和文字稿，"
        "对这个视频进行总结，格式需要使用简单的markdown格式，"
        f"需要保证清晰易读{length_req}。"
        "请注意：文字内容是通过视频音频转录来的，所以有可能有问题，"
        "如果遇到拼写偏差，请自行修正，并不要在总结内容中体现出来。"
    )


def _build_thinking_extra_body(model_id, enable_thinking):
    """思考深度控制按模型家族区分（都放 extra_body，兼容旧版 openai SDK）：
      - GPT 系列（gpt-*）：顶层 reasoning_effort（none/low/medium/high/xhigh/max），
        勾选深度思考映射 high，关闭映射 low；
      - Claude 系列（claude-*）：同样走 reasoning_effort，网关会映射为
        Anthropic 扩展思考（实测 high 会返回 reasoning_content，DeepSeek 式
        thinking 字段虽不报错但不生效）；
      - DeepSeek 系列：thinking 嵌套对象（OpenAI 标准 schema 无此字段），
        V4 默认开思考，必须显式传 disabled 才能关闭。
    """
    lowered = model_id.lower()
    if lowered.startswith(("gpt", "claude")):
        return {"reasoning_effort": "high" if enable_thinking else "low"}
    return {"thinking": {"type": "enabled" if enable_thinking else "disabled"}}


def list_available_models(base_url=None, api_key=None):
    """查询 OpenAI 兼容服务商的可用模型列表（设置弹窗下拉框用）。

    base_url / api_key 不传时用 .env 全局配置；查询失败时抛异常，
    由调用方回退到仅当前模型。
    """
    effective_key = api_key or LLM_API_KEY
    if not effective_key:
        raise ValueError("未配置 API Key。")

    from openai import OpenAI
    client = OpenAI(base_url=base_url or LLM_BASE_URL, api_key=effective_key)
    response = client.models.list()
    return sorted(model.id for model in response.data)


def summarize_content(title, text, progress_callback=None, enable_thinking=False,
                      model_id=None, word_limit=800, base_url=None, api_key=None):
    """使用 AI 模型总结内容（非流式）。

    enable_thinking: 是否启用深度思考（思维链推理）。默认关闭。
        - False: 关闭思考，响应快
        - True:  开启思考，回答更深入但更慢（思维链内容 reasoning_content 不返回/丢弃）
    model_id: 本次调用使用的模型 ID；None 用全局 MODEL_ID。
    word_limit: 总结篇幅上限（字）；None 或 0 表示不限制。
    base_url / api_key: 本次调用的服务商地址与密钥；None 用 .env 全局配置。
    """
    effective_key = api_key or LLM_API_KEY
    if not effective_key:
        raise ValueError("请先配置 API Key（设置弹窗或 .env）。")

    if progress_callback: progress_callback("正在调用AI模型进行总结...")

    effective_model = model_id or MODEL_ID
    from openai import OpenAI
    client = OpenAI(base_url=base_url or LLM_BASE_URL, api_key=effective_key)

    extra_body = _build_thinking_extra_body(effective_model, enable_thinking)

    try:
        response = client.chat.completions.create(
            model=effective_model,
            messages=[
                {"role": "system", "content": _build_system_prompt(word_limit)},
                {"role": "user", "content": f"视频标题：{title}\n\n文字稿：{text}"}
            ],
            stream=False,
            extra_body=extra_body,
        )
        return response.choices[0].message.content
    except Exception as e:
        raise LLMServiceError(_format_llm_error(e))


def summarize_content_stream(title, text, progress_callback=None, enable_thinking=False,
                             model_id=None, word_limit=800, base_url=None, api_key=None):
    """使用 AI 模型总结内容（流式输出）。

    enable_thinking: 是否启用深度思考。开启后流式响应里会先吐
        reasoning_content（思维链过程），再吐 content（最终总结）。这里只
        透传 content，思维链过程不展示（如需展示可读取 delta.reasoning_content）。
    model_id: 本次调用使用的模型 ID；None 用全局 MODEL_ID。
    word_limit: 总结篇幅上限（字）；None 或 0 表示不限制。
    base_url / api_key: 本次调用的服务商地址与密钥；None 用 .env 全局配置。
    """
    effective_key = api_key or LLM_API_KEY
    if not effective_key:
        raise ValueError("请先配置 API Key（设置弹窗或 .env）。")

    if progress_callback: progress_callback("正在调用AI模型进行总结...")

    effective_model = model_id or MODEL_ID
    from openai import OpenAI
    client = OpenAI(base_url=base_url or LLM_BASE_URL, api_key=effective_key)

    extra_body = _build_thinking_extra_body(effective_model, enable_thinking)

    try:
        response = client.chat.completions.create(
            model=effective_model,
            messages=[
                {"role": "system", "content": _build_system_prompt(word_limit)},
                {"role": "user", "content": f"视频标题：{title}\n\n文字稿：{text}"}
            ],
            stream=True,
            extra_body=extra_body,
        )

        for chunk in response:
            # 只取 delta.content（最终总结），忽略 delta.reasoning_content（思维链过程）
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        raise LLMServiceError(_format_llm_error(e))


_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")


def _normalize_list_blank_lines(md):
    """列表与相邻段落之间补空行（仅动空白，不改任何文字）。

    模型偶尔把列表紧跟在引导句后（无空行），markdown2 遵循老式
    markdown 规范会把「- 」按字面文本留在段落里，页面上就是一串
    减号。补空行后列表/段落各自成块，渲染恢复正常。代码块内容、
    嵌套列表的缩进续行不受影响。
    """
    lines = md.split("\n")
    out = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not stripped or line[0].isspace():
            out.append(line)  # 代码块内/空行/缩进续行（嵌套列表、条目续文）
            continue
        prev = next((l for l in reversed(out) if l.strip()), "")
        if not prev or prev.startswith((">", "|", "#", "```")):
            out.append(line)
            continue
        is_list = bool(_LIST_LINE_RE.match(line))
        prev_is_list = bool(_LIST_LINE_RE.match(prev))
        # 只在两行紧邻（前一行非空）时补空行，已是空行分隔的不再重复插
        if out[-1].strip() and is_list != prev_is_list:
            out.append("")  # 列表 ↔ 段落 交界处补空行
        out.append(line)
    return "\n".join(out)


def format_summary_markdown(title, summary_md, video_url):
    """统一总结最终版式：正文（含标题）完全由模型输出 + 视频链接收尾。

    网页阅读区、导出长图、落盘文件三处共用。程序不添加、不纠正标题
    （title 参数仅为保持调用签名兼容）；结尾拼接分隔线与视频链接，
    并对正文做空行规范化（列表与段落交界补空行，仅空白变化）。
    """
    body = _normalize_list_blank_lines((summary_md or "").strip())
    parts = []
    if body:
        parts.append(body)
    if video_url:
        parts.append(f"---\n\n视频链接：[{video_url}]({video_url})")
    return "\n\n".join(parts)


def save_transcription(bvid, title, text, p=1):
    """落盘单个视频的转录稿，按 BV 号 + 分集号命名。

    在转写完成（第 3 步）后立即调用，使后续 AI 总结即使失败，下次提交
    同一视频也能跳过「获取信息 / 下载 / 转录」三步直接重试总结。命名采用
    {bvid}_p{p} 作为缓存键，与 load_cached_transcription 配套；文件内容
    格式与 save_results 写出的转录稿完全一致，便于回读解析。
    """
    intermediate_dir = "intermediate_files"
    os.makedirs(intermediate_dir, exist_ok=True)

    video_url = _resolve_bili_video_url(None, bvid, p)
    cache_key = f"{bvid}_p{p}"
    txt_path = os.path.join(intermediate_dir, f"{cache_key}_transcription.txt")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"视频标题: {title}\n视频链接: {video_url}\n\n转录内容:\n\n{text}")

    return txt_path


def load_cached_transcription(bvid, p=1):
    """读取已缓存的转录稿，按 BV 号 + 分集号定位。

    返回 (title, text)；文件不存在或解析失败时返回 (None, None)。标题从
    文件头部「视频标题:」行解析，正文从「转录内容:」标记之后截取——这两
    个标记正是 save_transcription / save_results 写入的固定格式，自洽。
    """
    cache_key = f"{bvid}_p{p}"
    txt_path = os.path.join("intermediate_files", f"{cache_key}_transcription.txt")

    if not os.path.exists(txt_path):
        return None, None

    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None, None

    title = None
    # 标题取首行「视频标题:」之后的内容
    for line in content.splitlines():
        if line.startswith("视频标题:"):
            title = line[len("视频标题:"):].strip()
            break

    marker = "转录内容:\n"
    idx = content.find(marker)
    text = content[idx + len(marker):].strip() if idx != -1 else None

    if not text:
        return None, None

    return title, text


def save_results(bvid, title, text, summary, p=1):
    """保存结果并清理多余缓存"""
    intermediate_dir = "intermediate_files"
    output_dir = "final_outputs"
    os.makedirs(intermediate_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    cache_key = f"{bvid}_p{p}"
    video_url = _resolve_bili_video_url(None, bvid, p)

    # 转录稿复用 save_transcription，保证与 load_cached_transcription 的命名 / 格式一致
    txt_path = save_transcription(bvid, title, text, p)

    md_path = os.path.join(output_dir, f"{cache_key}_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(format_summary_markdown(title, summary, video_url))

    # 限制缓存目录大小为 30MB
    limit_directory_size(intermediate_dir, 30 * 1024 * 1024)

    return txt_path, md_path


def limit_directory_size(directory, max_size_bytes):
    """限制目录大小，如果超过则删除旧文件"""
    try:
        files = []
        for f in os.listdir(directory):
            path = os.path.join(directory, f)
            if os.path.isfile(path):
                files.append((path, os.path.getmtime(path), os.path.getsize(path)))

        # 按修改时间排序（从旧到新）
        files.sort(key=lambda x: x[1])

        current_size = sum(f[2] for f in files)
        while current_size > max_size_bytes and files:
            oldest_file_path, _, file_size = files.pop(0)
            os.remove(oldest_file_path)
            current_size -= file_size
            print(f"已删除旧缓存文件以释放空间: {oldest_file_path}")
    except Exception as e:
        print(f"清理缓存目录失败: {e}")
