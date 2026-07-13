
import os
import re
import subprocess
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QListWidget,
    QProgressBar, QTextEdit, QFileDialog, QGroupBox, QMessageBox,
    QAbstractItemView, QCheckBox,
)
from imageio_ffmpeg import get_ffmpeg_exe


FFMPEG = get_ffmpeg_exe()


def get_video_info(filepath: str) -> dict:
    """用 ffmpeg 获取视频信息：时长、分辨率（从 stderr 解析）"""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")

    cmd = [FFMPEG, "-i", filepath]
    result = subprocess.run(cmd, capture_output=True,
                            encoding="utf-8", errors="replace")
    stderr = result.stderr

    info = {"width": 0, "height": 0, "duration": 0.0}

    # 解析时长
    for line in stderr.split("\n"):
        if "Duration:" in line:
            time_str = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = time_str.split(":")
            info["duration"] = float(h) * 3600 + float(m) * 60 + float(s)

    # 解析视频流分辨率: Stream #0:0(...): Video: ..., 1920x1080, ...
    for line in stderr.split("\n"):
        if "Stream #" in line and "Video:" in line:
            # 在 Video: 描述中查找 WxH 模式
            match = re.search(r"(\d{2,})x(\d{2,})", line)
            if match:
                info["width"] = int(match.group(1))
                info["height"] = int(match.group(2))
                break

    if info["width"] == 0 or info["height"] == 0:
        raise RuntimeError(
            f"无法获取视频分辨率: {os.path.basename(filepath)}\n"
            f"ffmpeg 输出:\n{stderr.strip()[-500:]}"
        )

    return info


def calc_480p_size(orig_w: int, orig_h: int) -> tuple:
    """按 480p 目标高度计算等比缩放后的宽高（偶数对齐）"""
    target_h = 480
    # 如果原视频高度 ≤ 480，不需要缩小
    if orig_h <= target_h:
        return orig_w, orig_h
    target_w = round(orig_w * target_h / orig_h)
    # h264 编码要求宽高为偶数
    if target_w % 2 != 0:
        target_w += 1
    return target_w, target_h


def get_video_duration(filepath: str) -> float:
    """用 ffmpeg 获取视频时长（秒），从 stderr 解析 Duration 字段"""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")

    cmd = [FFMPEG, "-i", filepath]
    # encoding 用 utf-8，errors=replace 防止 Windows 上特殊字符导致崩溃
    result = subprocess.run(cmd, capture_output=True,
                            encoding="utf-8", errors="replace")

    for line in result.stderr.split("\n"):
        if "Duration:" in line:
            # 格式:  Duration: 00:01:23.45, start: ...
            time_str = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = time_str.split(":")
            return float(h) * 3600 + float(m) * 60 + float(s)

    # 解析失败时输出完整 stderr 帮助排查
    raise RuntimeError(
        f"无法获取视频时长: {os.path.basename(filepath)}\n"
        f"ffmpeg 输出:\n{result.stderr.strip()[-500:]}"
    )


def split_video(input_path: str, output_dir: str, segment_duration: int,
                log_callback, downscale_to_480p: bool = False):
    """分割单个视频，返回成功生成的片段数"""
    filename = Path(input_path).stem
    duration = get_video_duration(input_path)

    if duration <= segment_duration:
        log_callback(f"  → 跳过 (时长 {duration:.1f}s ≤ {segment_duration}s)")
        return 0

    segment_count = int(duration // segment_duration)

    # 分辨率信息
    scale_filter = None
    if downscale_to_480p:
        info = get_video_info(input_path)
        orig_w, orig_h = info["width"], info["height"]
        new_w, new_h = calc_480p_size(orig_w, orig_h)
        if new_w != orig_w or new_h != orig_h:
            scale_filter = f"scale={new_w}:{new_h}"
            log_callback(f"  时长 {duration:.1f}s，切割 {segment_count} 段，降分辨率 {orig_w}x{orig_h} → {new_w}x{new_h}")
        else:
            log_callback(f"  时长 {duration:.1f}s，切割 {segment_count} 段 (分辨率 ≤ 480p，无需降低)")
    else:
        log_callback(f"  时长 {duration:.1f}s，切割 {segment_count} 段")

    success_count = 0
    for i in range(segment_count):
        start = i * segment_duration
        out_path = os.path.join(output_dir, f"{filename}_part{i + 1:03d}.mp4")

        cmd = [
            FFMPEG,
            "-y",
            "-ss", str(start),
            "-i", input_path,
            "-t", str(segment_duration),
        ]

        if scale_filter:
            # 需要重新编码，不能 stream copy
            cmd += ["-vf", scale_filter, "-c:v", "libx264", "-crf", "23", "-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]

        cmd.append(out_path)

        proc = subprocess.run(cmd, capture_output=True,
                              encoding="utf-8", errors="replace")

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            log_callback(f"    [{i + 1}/{segment_count}] {os.path.basename(out_path)} ✓")
            success_count += 1
        else:
            err_detail = proc.stderr.strip()[-500:] if proc.stderr else "ffmpeg 无输出"
            log_callback(f"    [{i + 1}/{segment_count}] ✗ 失败")
            mode = "scale+encode" if scale_filter else "copy"
            log_callback(f"      模式: {mode}")
            log_callback(f"      错误: {err_detail}")

    return success_count


# ─── Worker 线程 ───────────────────────────────────────────────

class SplitWorker(QThread):
    """后台处理线程，避免阻塞 GUI"""
    progress = pyqtSignal(int)       # 当前进度
    total = pyqtSignal(int)          # 总文件数
    log = pyqtSignal(str)            # 日志消息
    finished_signal = pyqtSignal()   # 完成信号

    def __init__(self, input_dir: str, output_dir: str, duration: int,
                 downscale_to_480p: bool = False):
        super().__init__()
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.duration = duration
        self.downscale_to_480p = downscale_to_480p

    def run(self):
        mp4_files = sorted(Path(self.input_dir).glob("*.mp4"))
        total_files = len(mp4_files)
        self.total.emit(total_files)

        if total_files == 0:
            self.log.emit("⚠ 输入文件夹中没有找到 MP4 文件")
            self.finished_signal.emit()
            return

        os.makedirs(self.output_dir, exist_ok=True)
        total_segments = 0

        for idx, filepath in enumerate(mp4_files):
            self.log.emit(f"处理: {filepath.name}")
            try:
                segments = split_video(
                    str(filepath), self.output_dir, self.duration,
                    lambda msg: self.log.emit(msg),
                    downscale_to_480p=self.downscale_to_480p,
                )
                total_segments += segments
            except Exception as e:
                self.log.emit(f"  ✗ 错误: {e}")

            self.progress.emit(idx + 1)

        self.log.emit(f"\n✓ 完成！共处理 {total_files} 个文件，生成 {total_segments} 个片段")
        self.finished_signal.emit()


# ─── 主窗口 ────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MP4 视频批量分割工具")
        self.setMinimumSize(680, 620)
        self._worker = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # ── 输入文件夹 ──
        gb_in = QGroupBox("输入文件夹")
        h_in = QHBoxLayout(gb_in)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("选择包含 MP4 文件的文件夹...")
        btn_in = QPushButton("浏览...")
        btn_in.clicked.connect(self._browse_input)
        h_in.addWidget(self.input_edit)
        h_in.addWidget(btn_in)
        layout.addWidget(gb_in)

        # ── 输出文件夹 ──
        gb_out = QGroupBox("输出文件夹")
        h_out = QHBoxLayout(gb_out)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择保存分割后文件的文件夹...")
        btn_out = QPushButton("浏览...")
        btn_out.clicked.connect(self._browse_output)
        h_out.addWidget(self.output_edit)
        h_out.addWidget(btn_out)
        layout.addWidget(gb_out)

        # ── 分割时长 ──
        gb_dur = QGroupBox("分割设置")
        h_dur = QHBoxLayout(gb_dur)
        h_dur.addWidget(QLabel("每段时长（秒）："))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 99999)
        self.duration_spin.setValue(60)
        self.duration_spin.setSuffix(" 秒")
        h_dur.addWidget(self.duration_spin)
        h_dur.addStretch()

        h_dur.addWidget(QLabel("提示：剩余时长不足指定时长会自动丢弃"))
        h_dur.addStretch()
        layout.addWidget(gb_dur)

        # ── 分辨率选项 ──
        gb_res = QGroupBox("分辨率处理")
        h_res = QHBoxLayout(gb_res)
        self.downscale_check = QCheckBox("降低至 480P（标清）/ 保留原长宽比例")
        self.downscale_check.setToolTip("开启后视频将等比缩放至高度480像素，使用 H.264 重新编码")
        h_res.addWidget(self.downscale_check)
        h_res.addStretch()
        layout.addWidget(gb_res)

        # ── 文件列表 + 按钮 ──
        gb_files = QGroupBox("待处理文件")
        v_files = QVBoxLayout(gb_files)
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.file_list.setMaximumHeight(140)
        v_files.addWidget(self.file_list)

        h_btns = QHBoxLayout()
        self.scan_btn = QPushButton("🔍 扫描文件")
        self.scan_btn.clicked.connect(self._scan_files)
        h_btns.addWidget(self.scan_btn)

        h_btns.addStretch()

        self.start_btn = QPushButton("▶ 开始分割")
        self.start_btn.clicked.connect(self._start_split)
        self.start_btn.setEnabled(False)
        self.start_btn.setStyleSheet("QPushButton { font-weight: bold; min-height: 30px; }")
        h_btns.addWidget(self.start_btn)
        v_files.addLayout(h_btns)
        layout.addWidget(gb_files)

        # ── 进度条 ──
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        # ── 日志 ──
        gb_log = QGroupBox("处理日志")
        v_log = QVBoxLayout(gb_log)
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFont(QFont("Consolas", 9))
        self.log_area.setMaximumHeight(200)
        v_log.addWidget(self.log_area)
        layout.addWidget(gb_log)

    # ── 槽函数 ─────────────────────────────────────────────────

    def _browse_input(self):
        path = QFileDialog.getExistingDirectory(self, "选择输入文件夹")
        if path:
            self.input_edit.setText(path)
            self._scan_files()

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if path:
            self.output_edit.setText(path)

    def _scan_files(self):
        self.file_list.clear()
        input_dir = self.input_edit.text().strip()
        if not input_dir or not os.path.isdir(input_dir):
            self.start_btn.setEnabled(False)
            return

        mp4_files = sorted(Path(input_dir).glob("*.mp4"))
        if not mp4_files:
            self.file_list.addItem("未找到 MP4 文件")
            self.start_btn.setEnabled(False)
        else:
            for f in mp4_files:
                self.file_list.addItem(f"  📄 {f.name}")
            self.start_btn.setEnabled(True)

        self.log_area.clear()

    def _start_split(self):
        input_dir = self.input_edit.text().strip()
        output_dir = self.output_edit.text().strip()

        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(self, "警告", "请先选择有效的输入文件夹")
            return
        if not output_dir:
            QMessageBox.warning(self, "警告", "请先选择输出文件夹")
            return

        # 禁用按钮，防止重复点击
        self.start_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.log_area.clear()
        self.progress_bar.setValue(0)

        self._worker = SplitWorker(input_dir, output_dir, self.duration_spin.value(),
                                   downscale_to_480p=self.downscale_check.isChecked())
        self._worker.total.connect(self.progress_bar.setMaximum)
        self._worker.progress.connect(self.progress_bar.setValue)
        self._worker.log.connect(self._append_log)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.start()

    def _append_log(self, msg: str):
        self.log_area.append(msg)
        # 自动滚到底部
        scrollbar = self.log_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_finished(self):
        self.start_btn.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.file_list.clear()
        self._scan_files()


# ─── 入口 ──────────────────────────────────────────────────────

def main():
    app = QApplication([])
    win = MainWindow()
    win.show()
    app.exec_()


if __name__ == "__main__":
    main()
