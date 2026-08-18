# -*- coding: utf-8 -*-
"""ASR 子进程隔离：funasr/torch 只在独立工作进程里加载，主进程保持轻量。

背景（见 bili_core.py 头部注释 / AGENTS.md 陷阱 1）：历史上 Windows 上
bilibili_api(curl_cffi) / modelscope 先于 funasr 加载会导致 funasr 内部
torch.jit 编译段错误，因此 funasr 必须最先导入；这迫使任何 import
bili_core 的进程都连带加载 torch（约 10-30 秒）。本模块用进程隔离根治：

  - 子进程（`python asr_worker.py`）：唯一允许加载 funasr 的地方。该进程
    永不 import bilibili_api / modelscope，导入顺序约束天然满足。协议为
    stdin/stdout 上的 JSON 行（stderr 继承父进程，承接 funasr 的进度条）。
  - 主进程（web_ui / api）：import 本模块（纯标准库，轻量），通过
    get_asr_worker() 拿到客户端单例，调 preload()/transcribe()/status()。
    主进程从头到尾不会加载 torch。

请求（stdin，一行一个 JSON）：
  {"id": 1, "cmd": "preload"}
  {"id": 2, "cmd": "transcribe", "path": "xxx.m4a"}
  {"id": 3, "cmd": "shutdown"}
响应（stdout，一行一个 JSON）：
  {"type": "status", "state": "loading|ready|error"}          # 无 id，状态广播
  {"id": 2, "type": "progress", "msg": "正在转写..."}
  {"id": 2, "type": "result", "text": "……"}                    # transcribe 完成
  {"id": 1, "type": "done"}                                    # preload/shutdown 完成
  {"id": 2, "type": "error", "error": "中文错误信息"}

自测：python asr_worker.py selftest [--audio benchmark/test.wav]
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
_WORKER_SCRIPT = Path(__file__).resolve()

# 状态机：idle（未启动）→ starting（进程已拉起）→ loading（模型加载中）
# → ready（就绪）；error（加载/运行报错）、dead（进程退出，下次调用自动重启）
_STATE_IDLE = "idle"
_STATE_STARTING = "starting"
_STATE_LOADING = "loading"
_STATE_READY = "ready"
_STATE_ERROR = "error"
_STATE_DEAD = "dead"

# Windows 上父进程若是 pythonw（无控制台），子进程 python 会弹黑框，必须屏蔽
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# 子进程侧：协议处理循环
# ---------------------------------------------------------------------------

def _child_main():
    """工作进程主循环：读 stdin 命令，复用 bili_core 完成加载/转录。"""
    # 协议输出独占 fd 1 的副本；fd 1 本身重定向到 stderr，
    # 拦截 funasr/torch 等第三方库意外打到 stdout 的输出，防止污染协议
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8")
    try:
        os.dup2(2, 1)
    except OSError:
        pass
    sys.stdout = sys.stderr
    # 父进程按 UTF-8 写入请求行；Windows 子进程默认 stdin 编码是本地码页，需对齐
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def send(obj):
        proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proto.flush()

    # 延迟导入：保证本模块被主进程 import 时零重型依赖
    import bili_core

    state = _STATE_IDLE

    def set_state(new):
        nonlocal state
        state = new
        send({"type": "status", "state": new})

    send({"type": "status", "state": state})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        cmd = req.get("cmd")

        if cmd == "preload":
            set_state(_STATE_LOADING)
            try:
                bili_core.preload_asr_model(
                    lambda m: send({"id": rid, "type": "progress", "msg": m})
                )
                set_state(_STATE_READY)
                send({"id": rid, "type": "done"})
            except Exception as e:
                set_state(_STATE_ERROR)
                send({"id": rid, "type": "error", "error": f"ASR 引擎加载失败: {e}"})

        elif cmd == "transcribe":
            if state != _STATE_READY:
                # 冷启动：transcribe_audio 内部会先加载模型
                set_state(_STATE_LOADING)
            try:
                text = bili_core.transcribe_audio(
                    req.get("path", ""),
                    lambda m: send({"id": rid, "type": "progress", "msg": m}),
                )
                if state != _STATE_READY:
                    set_state(_STATE_READY)
                send({"id": rid, "type": "result", "text": text})
            except Exception as e:
                send({"id": rid, "type": "error", "error": str(e)})

        elif cmd == "shutdown":
            send({"id": rid, "type": "done"})
            break

    # stdin EOF = 父进程已退出（含 os._exit 自动退出场景），模型随本进程释放
    proto.close()


# ---------------------------------------------------------------------------
# 主进程侧：客户端单例
# ---------------------------------------------------------------------------

class _Pending:
    """一次请求的应答信箱：reader 线程投递，调用线程取件。"""

    def __init__(self, progress_callback=None):
        self.event = threading.Event()
        self.text = None
        self.error = None
        self.progress = progress_callback


class AsrWorkerClient:
    """ASR 工作进程客户端：一个主进程一个 worker，请求串行处理。

    worker 意外退出（dead）时，下一次 transcribe 会自动重启 worker
    并重载模型（首次约需半分钟），调用方无需感知重启细节。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._req_lock = threading.Lock()  # 串行化完整请求-应答（worker 单线程）
        self._proc = None
        self._state = _STATE_IDLE
        self._pending = {}
        self._next_id = 1

    # -- 生命周期 -----------------------------------------------------------

    def start(self):
        """拉起工作进程（已存活则跳过）。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._proc = subprocess.Popen(
                [sys.executable, str(_WORKER_SCRIPT)],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # 继承父进程 stderr（日志文件/控制台）
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_CREATE_NO_WINDOW,
            )
            self._state = _STATE_STARTING
            threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True).start()

    def preload(self):
        """非阻塞预热：发一条 preload 命令即返回，加载进度经 status 体现。"""
        self._request("preload", wait=False)

    def shutdown(self):
        """优雅停止（主进程退出前调用；忘调也无妨，worker 会随 stdin EOF 自尽）。"""
        try:
            self._request("shutdown", timeout=10)
        except Exception:
            pass
        with self._lock:
            proc, self._proc = self._proc, None
            self._state = _STATE_IDLE
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def status(self):
        """当前状态字符串（idle/starting/loading/ready/error/dead），只读不拉起。"""
        with self._lock:
            return self._state

    # -- 业务 ---------------------------------------------------------------

    def transcribe(self, audio_path, progress_callback=None):
        """请求转录并阻塞等待结果（在后台线程里调用）。"""
        result = self._request("transcribe", {"path": str(audio_path)},
                               progress_callback=progress_callback)
        return result["text"]

    # -- 内部 ---------------------------------------------------------------

    def _write(self, obj):
        line = json.dumps(obj) + "\n"  # 默认 ensure_ascii=True，纯 ASCII 行最稳
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise RuntimeError("ASR 工作进程未运行")
            proc.stdin.write(line)
            proc.stdin.flush()

    def _request(self, cmd, extra=None, wait=True, timeout=None,
                 progress_callback=None):
        """发送一条命令。wait=True 时阻塞到应答并返回应答 dict。"""
        with self._req_lock:
            # worker 已死则先重启（模型重载，耗时可观但保证了自愈）
            if self.status() == _STATE_DEAD:
                with self._lock:
                    if self._proc is not None:
                        try:
                            self._proc.kill()
                        except Exception:
                            pass
                        self._proc = None
                self.start()

            self.start()
            with self._lock:
                rid = self._next_id
                self._next_id += 1
            pending = _Pending(progress_callback)
            with self._lock:
                self._pending[rid] = pending
            req = {"id": rid, "cmd": cmd}
            if extra:
                req.update(extra)
            try:
                self._write(req)
            except Exception:
                with self._lock:
                    self._pending.pop(rid, None)
                raise

            if not wait:
                return None
            if not pending.event.wait(timeout):
                with self._lock:
                    self._pending.pop(rid, None)
                raise RuntimeError(f"ASR 工作进程响应超时（{cmd}）")
            if pending.error is not None:
                raise RuntimeError(pending.error)
            return {"text": pending.text}

    def _read_loop(self, proc):
        """常驻 reader 线程：解析 worker 的 JSON 行，分发状态/进度/应答。"""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "status":
                with self._lock:
                    self._state = msg.get("state", self._state)
                continue
            rid = msg.get("id")
            with self._lock:
                terminal = mtype in ("result", "done", "error")
                pending = self._pending.pop(rid, None) if terminal else self._pending.get(rid)
            if pending is None:
                continue
            if mtype == "progress":
                if pending.progress:
                    try:
                        pending.progress(msg.get("msg", ""))
                    except Exception:
                        pass  # 进度回调异常不应杀死 reader（含取消抛错场景）
            elif mtype == "result":
                pending.text = msg.get("text", "")
                pending.event.set()
            elif mtype == "done":
                pending.event.set()
            elif mtype == "error":
                pending.error = msg.get("error", "未知错误")
                pending.event.set()
        # EOF：worker 进程结束，叫醒所有等待者
        with self._lock:
            self._state = _STATE_DEAD
            for pending in self._pending.values():
                pending.error = "ASR 工作进程意外退出，请重试"
                pending.event.set()
            self._pending.clear()


_worker_instance = None
_worker_singleton_lock = threading.Lock()


def get_asr_worker():
    """进程内共享的客户端单例（对应旧架构里的 ASR 模型单例，勿按页面各建一份）。"""
    global _worker_instance
    if _worker_instance is None:
        with _worker_singleton_lock:
            if _worker_instance is None:
                _worker_instance = AsrWorkerClient()
    return _worker_instance


# ---------------------------------------------------------------------------
# 自测：python asr_worker.py selftest
# ---------------------------------------------------------------------------

def _selftest(audio_path):
    w = get_asr_worker()
    print("拉起 ASR 工作进程并预热…")
    w.preload()
    deadline = time.time() + 300  # 冷启动含模型加载，留足 5 分钟
    while time.time() < deadline:
        st = w.status()
        if st in (_STATE_READY, _STATE_ERROR, _STATE_DEAD):
            break
        print(f"  状态: {st}")
        time.sleep(2)
    st = w.status()
    if st != _STATE_READY:
        w.shutdown()
        sys.exit(f"自测失败：worker 状态 {st}")
    print("worker 已就绪，开始转录…")

    t0 = time.time()
    text = w.transcribe(audio_path, lambda m: print(f"  [进度] {m}"))
    print(f"转录完成，耗时 {time.time() - t0:.1f} 秒，结果前 80 字：")
    print(f"  {text[:80]}")

    # 关键断言：主进程全程没碰 torch/funasr，隔离成立
    leaked = [m for m in sys.modules if m.split(".")[0] in ("torch", "funasr")]
    w.shutdown()
    if leaked:
        sys.exit(f"自测失败：主进程加载了 {leaked}，隔离被破坏")
    print("自测通过：主进程未加载 torch/funasr，转录全程在子进程完成")


def main():
    parser = argparse.ArgumentParser(description="ASR 子进程隔离 worker")
    parser.add_argument(
        "mode", nargs="?", default="worker",
        help="worker=作为子进程运行（默认）；selftest=端到端自测",
    )
    parser.add_argument(
        "--audio", default=str(PROJECT_ROOT / "benchmark" / "test.wav"),
        help="selftest 用的音频路径",
    )
    args = parser.parse_args()
    if args.mode == "selftest":
        _selftest(args.audio)
    else:
        _child_main()


if __name__ == "__main__":
    main()
