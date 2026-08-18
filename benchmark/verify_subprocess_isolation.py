# -*- coding: utf-8 -*-
"""验证「ASR 子进程隔离」能否绕开 Windows 上的 funasr 导入顺序段错误。

背景（见 bili_core.py 顶部注释 / AGENTS.md 陷阱 1）：同一进程内若
bilibili_api(curl_cffi) 或 modelscope 先于 funasr 加载，funasr 内部
torch.jit 编译（funasr/models/*/cif_predictor.py）会触发 access
violation，直接杀死进程。因此 bili_core 顶层必须最先 import funasr，
代价是任何入口导入 bili_core 都要连带加载 torch（约 10-30 秒）。

本脚本用三种模式做对照实验（务必各自在全新进程中运行）：

  control   bili_core 最先导入（funasr 先就位，生产现状）→ 预期成功
  bad       先导入投毒模块（bilibili_api / modelscope / 两者），
            再 import bili_core 走转录 → 预期段错误（退出码 0xC0000005=3221225477）
  isolate   主进程先导入全部投毒模块（模拟字幕/视频信息流程已运行），
            然后用 multiprocessing spawn 起子进程，子进程内 import
            bili_core 完成转录并把文字经管道传回 → 预期成功

用法：
  python benchmark/verify_subprocess_isolation.py control
  python benchmark/verify_subprocess_isolation.py bad --poison both
  python benchmark/verify_subprocess_isolation.py bad --poison bilibili
  python benchmark/verify_subprocess_isolation.py isolate
  python benchmark/verify_subprocess_isolation.py run-all   # 编排上面全部

注意：重模块（bili_core/bilibili_api/modelscope）只在函数内导入，
模块顶层保持轻量——spawn 子进程会重新 import 本模块。
"""

import argparse
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

# spawn 子进程重新 import 本模块时，sys.path[0] 是 benchmark/，
# 需要把项目根目录补进去才能 import bili_core
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_AUDIO = str(Path(__file__).resolve().parent / "test.wav")


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _transcribe_inprocess(audio_path):
    """在当前进程内走 bili_core 生产路径转录（导入顺序由调用方事先决定）。"""
    import bili_core  # noqa: F401  顶层会按需先 import funasr

    _log("bili_core 导入完成，开始转录…")
    t0 = time.time()
    text = bili_core.transcribe_audio(audio_path, lambda m: _log(f"  {m}"))
    _log(f"转录完成，耗时 {time.time() - t0:.1f} 秒")
    print(f"转写结果: {text!r}", flush=True)
    return text


def _poison(kind):
    """按指定组合先加载会"污染"进程原生 DLL 状态的模块。"""
    if kind in ("bilibili", "both"):
        import bilibili_api  # noqa: F401  连带加载 curl_cffi 原生模块

        _log(f"已导入 bilibili_api（curl_cffi 原生模块: {'curl_cffi' in sys.modules}）")
    if kind in ("modelscope", "both"):
        import modelscope  # noqa: F401  连带加载 torch 等重型依赖

        _log("已导入 modelscope（含 torch）")


def _isolation_child(audio_path, conn):
    """spawn 子进程入口：全新解释器里 import bili_core 转录，结果传回主进程。"""
    try:
        import bili_core

        text = bili_core.transcribe_audio(audio_path)
        conn.send({"ok": True, "text": text})
    except Exception as e:  # 子进程任何异常都以结构化数据传回，不让主进程猜
        import traceback

        conn.send({"ok": False, "error": f"{e}\n{traceback.format_exc()}"})
    finally:
        conn.close()


def run_control(audio_path):
    _poison("none")
    _transcribe_inprocess(audio_path)


def run_bad(audio_path, poison):
    _poison(poison)
    _log("投毒完成，现在 import bili_core（funasr 将后加载）…")
    _transcribe_inprocess(audio_path)


def run_isolate(audio_path):
    # 主进程模拟"字幕/视频信息流程已运行"的状态：先吃进全部投毒模块
    _poison("both")
    _log("主进程已投毒，spawn 子进程开始隔离转录…")

    ctx = multiprocessing.get_context("spawn")  # 全新解释器，不继承主进程 DLL 状态
    parent_conn, child_conn = ctx.Pipe()
    t0 = time.time()
    proc = ctx.Process(target=_isolation_child, args=(audio_path, child_conn))
    proc.start()
    result = parent_conn.recv()  # 阻塞等待子进程回传（子进程崩溃时 recv 会 EOFError）
    proc.join(30)
    _log(f"子进程结束（耗时 {time.time() - t0:.1f} 秒，exitcode={proc.exitcode}）")

    if result.get("ok"):
        print(f"转写结果: {result['text']!r}", flush=True)
        _log("隔离验证成功：主进程虽已加载 curl_cffi/torch，子进程转录未受影响")
    else:
        print(f"子进程报错:\n{result['error']}", flush=True)
        sys.exit(2)


def gen_test_audio():
    """test.wav 缺失时用 Windows 自带 TTS 生成一段中文语音兜底。"""
    out = Path(DEFAULT_AUDIO)
    if out.exists():
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try { $v = $s.GetInstalledVoices() | Where-Object "
        "{$_.VoiceInfo.Culture -like 'zh*'} | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) } } catch {};"
        f"$s.SetOutputToWaveFile('{out}');"
        "$s.Speak('大家好，今天我们来验证语音识别的子进程隔离方案。');"
        "$s.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True
    )


MODES = ("control", "bad", "isolate")


def run_all(audio_path):
    """编排全部实验：每个模式单独起进程跑，汇总退出码。"""
    cases = [
        ("control", ["control"]),
        ("bad-both", ["bad", "--poison", "both"]),
        ("bad-bilibili", ["bad", "--poison", "bilibili"]),
        ("bad-modelscope", ["bad", "--poison", "modelscope"]),
        ("isolate", ["isolate"]),
    ]
    results = []
    for name, args in cases:
        _log(f"=== 开始 {name} ===")
        p = subprocess.run(
            [sys.executable, os.path.abspath(__file__), *args, "--audio", audio_path],
            capture_output=True, text=True, timeout=420, cwd=str(PROJECT_ROOT),
        )
        tail = (p.stdout or "").strip().splitlines()[-3:]
        print(f"--- {name}: 退出码 {p.returncode} ---", flush=True)
        for line in tail:
            print(f"    {line}", flush=True)
        if p.returncode not in (0,) and p.stderr:
            print(f"    stderr尾行: {p.stderr.strip().splitlines()[-1]}", flush=True)
        results.append((name, p.returncode))

    print("\n===== 汇总（0xC0000005 = 3221225477 = 段错误）=====", flush=True)
    for name, code in results:
        print(f"  {name:16s} 退出码 {code}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="funasr 导入顺序段错误隔离验证")
    parser.add_argument("mode", nargs="?", default="run-all", choices=(*MODES, "run-all"))
    parser.add_argument("--poison", default="both", choices=("bilibili", "modelscope", "both"))
    parser.add_argument("--audio", default=DEFAULT_AUDIO)
    args = parser.parse_args()

    if not Path(args.audio).exists():
        if args.audio == DEFAULT_AUDIO:
            gen_test_audio()
        else:
            sys.exit(f"测试音频不存在: {args.audio}")

    if args.mode == "control":
        run_control(args.audio)
    elif args.mode == "bad":
        run_bad(args.audio, args.poison)
    elif args.mode == "isolate":
        run_isolate(args.audio)
    else:
        run_all(args.audio)


if __name__ == "__main__":
    main()
