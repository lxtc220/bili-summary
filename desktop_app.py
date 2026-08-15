"""B站视频总结助手桌面版入口。

PySide6 进程只负责界面和用户交互，不直接导入 bili_core、FunASR 或 Torch。
所有耗时业务都通过 QProcess 交给 desktop_engine.py，避免首次加载模型冻结窗口。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QProcess, QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


def _resolve_project_root() -> Path:
    """返回程序根目录；源码运行与 PyInstaller 目录版运行分别处理。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_ROOT = _resolve_project_root()
# 后台引擎位于 package_root/engine，但业务数据应统一落在 package_root。
os.environ.setdefault("BILI_SUMMARY_ROOT", str(PROJECT_ROOT))


def _default_log_path() -> Path:
    preferred_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BiliSummary" / "logs"
    try:
        preferred_dir.mkdir(parents=True, exist_ok=True)
        return preferred_dir / "engine.log"
    except OSError:
        return PROJECT_ROOT / "runtime_logs" / "engine.log"


DEFAULT_LOG_PATH = _default_log_path()


class MainWindow(QMainWindow):
    """桌面主窗口，所有用户可见文本均为中文。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("B站视频总结助手")
        self.resize(1080, 760)

        self.engine = QProcess(self)
        self.engine.setProcessChannelMode(QProcess.SeparateChannels)
        self.engine.readyReadStandardOutput.connect(self._read_engine_output)
        self.engine.readyReadStandardError.connect(self._read_engine_error)
        self.engine.started.connect(self._engine_started)
        self.engine.finished.connect(self._engine_finished)
        self.engine.errorOccurred.connect(self._engine_error)

        self._stdout_buffer = QByteArray()
        self._stderr_tail: list[str] = []
        self._closing = False
        self._engine_ready = False
        self._busy = False
        self._restart_count = 0
        self._last_summary_path = ""
        self._last_transcription_path = ""

        self._build_ui()
        self._start_engine()

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        title = QLabel("B站视频总结助手")
        title.setStyleSheet("font-size: 26px; font-weight: 700;")
        layout.addWidget(title)

        self.engine_status = QLabel("后台引擎：正在启动...")
        self.engine_status.setStyleSheet("color: #64748b;")
        layout.addWidget(self.engine_status)

        input_box = QGroupBox("视频任务")
        input_layout = QVBoxLayout(input_box)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("视频链接："))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("粘贴 B 站视频链接，例如 https://www.bilibili.com/video/BV...")
        self.url_edit.returnPressed.connect(self.start_processing)
        url_row.addWidget(self.url_edit, 1)
        input_layout.addLayout(url_row)

        option_row = QHBoxLayout()
        self.thinking_check = QCheckBox("启用 AI 深度思考（更慢，但可能更详细）")
        option_row.addWidget(self.thinking_check)
        option_row.addStretch(1)
        self.start_button = QPushButton("开始处理")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_processing)
        option_row.addWidget(self.start_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_processing)
        option_row.addWidget(self.cancel_button)
        input_layout.addLayout(option_row)
        layout.addWidget(input_box)

        self.progress_label = QLabel("等待处理")
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.video_info = QLabel("")
        self.video_info.setWordWrap(True)
        self.video_info.setStyleSheet("color: #475569;")
        layout.addWidget(self.video_info)

        summary_box = QGroupBox("视频总结")
        summary_layout = QVBoxLayout(summary_box)
        self.summary_view = QTextBrowser()
        self.summary_view.setOpenExternalLinks(True)
        self.summary_view.setPlaceholderText("处理完成后，这里会显示 Markdown 总结。")
        # 流式分片的原文缓冲。分片阶段只向视图追加纯文本，等 result 事件
        # 到达后再用完整 Markdown 一次性 setMarkdown 渲染；逐片
        # "toPlainText + setMarkdown 重解析"会破坏文档结构（换行丢失、
        # # 残留），234 个分片累积后界面彻底失去排版。
        self._summary_buffer: list[str] = []
        summary_layout.addWidget(self.summary_view, 1)
        summary_button_row = QHBoxLayout()
        self.open_summary_button = QPushButton("打开 Markdown 文件")
        self.open_summary_button.setEnabled(False)
        self.open_summary_button.clicked.connect(self.open_summary_file)
        summary_button_row.addWidget(self.open_summary_button)
        self.open_transcription_button = QPushButton("打开转录文件")
        self.open_transcription_button.setEnabled(False)
        self.open_transcription_button.clicked.connect(self.open_transcription_file)
        summary_button_row.addWidget(self.open_transcription_button)
        summary_button_row.addStretch(1)
        summary_layout.addLayout(summary_button_row)
        layout.addWidget(summary_box, 1)

        self.log_label = QLabel(f"引擎日志：{DEFAULT_LOG_PATH}")
        self.log_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_label.setStyleSheet("color: #64748b; font-size: 11px;")
        layout.addWidget(self.log_label)

        self.setStyleSheet(
            """
            QMainWindow { background: #f8fafc; }
            QGroupBox { font-weight: 600; border: 1px solid #dbe3ee; border-radius: 8px; margin-top: 8px; padding-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLineEdit, QTextBrowser { background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px; }
            QPushButton { padding: 7px 14px; border: 1px solid #cbd5e1; border-radius: 6px; background: white; }
            QPushButton:disabled { color: #94a3b8; }
            QPushButton#primaryButton { color: white; background: #2563eb; border-color: #2563eb; font-weight: 600; }
            QPushButton#primaryButton:disabled { background: #93c5fd; border-color: #93c5fd; }
            """
        )

        file_menu = self.menuBar().addMenu("文件")
        open_log_action = QAction("打开引擎日志", self)
        open_log_action.triggered.connect(self.open_log_file)
        file_menu.addAction(open_log_action)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _engine_command(self) -> tuple[str, list[str]]:
        if getattr(sys, "frozen", False):
            package_root = Path(sys.executable).resolve().parent
            candidates = (
                package_root / "engine" / "BiliSummaryEngine.exe",
                package_root / "BiliSummaryEngine.exe",
            )
            engine_exe = next((path for path in candidates if path.exists()), candidates[0])
            return str(engine_exe), ["--engine"]
        return sys.executable, [str(PROJECT_ROOT / "desktop_engine.py"), "--engine"]

    def _start_engine(self) -> None:
        if self._closing:
            return
        if self.engine.state() != QProcess.NotRunning:
            return
        program, arguments = self._engine_command()
        self._engine_ready = False
        self.engine_status.setText("后台引擎：正在启动，正在预热核心组件...")
        self.start_button.setEnabled(False)
        self.engine.setWorkingDirectory(str(PROJECT_ROOT))
        self.engine.setProgram(program)
        self.engine.setArguments(arguments)
        self.engine.start()

    def _engine_started(self) -> None:
        self.engine_status.setText("后台引擎：正在预热语音识别模型...")

    def _read_engine_output(self) -> None:
        self._stdout_buffer.append(self.engine.readAllStandardOutput())
        while True:
            newline_index = self._stdout_buffer.indexOf(b"\n")
            if newline_index < 0:
                return
            raw_line = bytes(self._stdout_buffer[:newline_index])
            self._stdout_buffer.remove(0, newline_index + 1)
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                self._stderr_tail.append(raw_line.decode("utf-8", errors="replace"))
                self._stderr_tail = self._stderr_tail[-20:]
                continue
            self._handle_engine_event(event)

    def _read_engine_error(self) -> None:
        text = bytes(self.engine.readAllStandardError()).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip():
                self._stderr_tail.append(line.strip())
        self._stderr_tail = self._stderr_tail[-20:]

    def _handle_engine_event(self, event: dict) -> None:
        event_name = event.get("event")
        if event.get("log_path"):
            self.log_label.setText(f"引擎日志：{event['log_path']}")

        if event_name == "status":
            self.engine_status.setText(f"后台引擎：{event.get('message', '')}")
        elif event_name == "ready":
            self._engine_ready = True
            self._restart_count = 0
            self.engine_status.setText("后台引擎：已就绪")
            self.start_button.setEnabled(not self._busy)
        elif event_name == "stage":
            self.progress_label.setText(event.get("message", "正在处理..."))
        elif event_name == "progress":
            self.progress_label.setText(event.get("message", "正在处理..."))
        elif event_name == "video_info":
            info = event.get("info") or {}
            title = info.get("title") or "未知标题"
            owner = info.get("owner") or "未知 UP 主"
            page_count = info.get("page_count") or 1
            self.video_info.setText(f"标题：{title}　UP主：{owner}　分集数：{page_count}")
        elif event_name == "summary_chunk":
            self._append_summary(event.get("content", ""))
        elif event_name == "result":
            self._busy = False
            self._set_processing(False)
            self._render_summary(event.get("summary", ""))
            self._last_summary_path = event.get("summary_path", "")
            self._last_transcription_path = event.get("transcription_path", "")
            self.open_summary_button.setEnabled(bool(self._last_summary_path))
            self.open_transcription_button.setEnabled(bool(self._last_transcription_path))
            self.progress_label.setText(
                f"处理完成，用时 {event.get('elapsed', 0)} 秒。"
                + ("（复用了转录缓存）" if event.get("cache_hit") else "")
            )
            self.engine_status.setText("后台引擎：已就绪")
        elif event_name == "cancelled":
            self._busy = False
            self._set_processing(False)
            self.progress_label.setText(event.get("message", "已取消当前任务。"))
        elif event_name == "error":
            self._busy = False
            self._set_processing(False)
            message = event.get("message", "后台处理失败。")
            if event.get("log_path"):
                self.log_label.setText(f"引擎日志：{event['log_path']}")
            self.progress_label.setText(message)
            if event.get("fatal"):
                self._engine_ready = False
                self.engine_status.setText("后台引擎：启动失败")
                self.start_button.setEnabled(False)

    def _append_summary(self, content: str) -> None:
        """流式阶段以纯文本追加显示，保持分片原文不经过 Markdown round-trip。"""
        if not content:
            return
        self._summary_buffer.append(content)
        cursor = self.summary_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(content)
        self.summary_view.setTextCursor(cursor)
        scrollbar = self.summary_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _render_summary(self, summary: str) -> None:
        """任务完成后按 Markdown 一次性渲染最终排版。"""
        if hasattr(self.summary_view, "setMarkdown"):
            self.summary_view.setMarkdown(summary)
        else:
            self.summary_view.setPlainText(summary)
        scrollbar = self.summary_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_processing(self, processing: bool) -> None:
        self.start_button.setEnabled(self._engine_ready and not processing)
        self.cancel_button.setEnabled(processing)
        self.url_edit.setEnabled(not processing)
        self.thinking_check.setEnabled(not processing)
        self.progress_bar.setVisible(processing)

    def start_processing(self) -> None:
        if not self._engine_ready or self._busy:
            return
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先粘贴 B 站视频链接。")
            return
        self._busy = True
        self._set_processing(True)
        self.progress_label.setText("正在提交任务...")
        self.video_info.clear()
        self.summary_view.clear()
        self._summary_buffer.clear()
        command = {
            "command": "process",
            "url": url,
            "enable_thinking": self.thinking_check.isChecked(),
        }
        self._send_command(command)

    def cancel_processing(self) -> None:
        if self._busy:
            self._send_command({"command": "cancel"})
            self.cancel_button.setEnabled(False)

    def _send_command(self, command: dict) -> None:
        if self.engine.state() == QProcess.Running:
            payload = (json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8")
            self.engine.write(payload)
            self.engine.waitForBytesWritten(1000)

    def _engine_finished(self, exit_code: int, _exit_status) -> None:
        if self._closing:
            return
        self._engine_ready = False
        self.start_button.setEnabled(False)
        if self._restart_count < 1:
            self._restart_count += 1
            self.engine_status.setText("后台引擎异常，正在自动重启（1/1）...")
            QTimer.singleShot(1000, self._start_engine)
        else:
            self.engine_status.setText("后台引擎：启动失败")
            self.progress_label.setText(
                f"后台引擎已退出（代码 {exit_code}）。请查看日志：{DEFAULT_LOG_PATH}"
            )

    def _engine_error(self, _error) -> None:
        if not self._closing:
            self.engine_status.setText("后台引擎：启动异常，正在检查日志...")

    def open_summary_file(self) -> None:
        self._open_path(self._last_summary_path)

    def open_transcription_file(self) -> None:
        self._open_path(self._last_transcription_path)

    def open_log_file(self) -> None:
        self._open_path(str(DEFAULT_LOG_PATH))

    def _open_path(self, path: str) -> None:
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))
        else:
            QMessageBox.information(self, "提示", "文件尚未生成。")

    def closeEvent(self, event) -> None:
        self._closing = True
        if self.engine.state() == QProcess.Running:
            self._send_command({"command": "shutdown"})
            if not self.engine.waitForFinished(2000):
                self.engine.kill()
                self.engine.waitForFinished(1000)
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("BiliSummary")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
