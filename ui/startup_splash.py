"""Lightweight startup splash for immediate launch feedback."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path


SPLASH_ARG = "--splash-child"
SPLASH_PREFERRED_SIZE = (380, 168)
SPLASH_SCREEN_MARGIN = 16


def _splash_layout(screen_width: int, screen_height: int) -> tuple[int, int, int, int]:
    """Fit and centre the splash on very small displays."""

    screen_width = max(1, int(screen_width))
    screen_height = max(1, int(screen_height))
    available_width = max(1, screen_width - SPLASH_SCREEN_MARGIN * 2)
    available_height = max(1, screen_height - SPLASH_SCREEN_MARGIN * 2)
    width = min(SPLASH_PREFERRED_SIZE[0], available_width)
    height = min(SPLASH_PREFERRED_SIZE[1], available_height)
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    return width, height, x, y


class StartupSplash:
    """Controller for a small splash process shown while the app loads."""

    def __init__(self, enabled: bool = True):
        self._started_at = time.perf_counter()
        self._process: subprocess.Popen[str] | None = None

        if enabled and splash_process_supported():
            self._start_process()

    @property
    def visible(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def pulse(self, message: str | None = None) -> None:
        if message:
            self._send(f"STATUS\t{message}")

    def close(self) -> None:
        process = self._process
        self._process = None
        if not process:
            return

        self._send_to_process(process, "CLOSE")
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass

        try:
            process.wait(timeout=1.2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                process.kill()

    def keep_visible_for(self, min_seconds: float) -> None:
        while self.visible and time.perf_counter() - self._started_at < min_seconds:
            time.sleep(0.03)

    def _start_process(self) -> None:
        command = _splash_command()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=_splash_subprocess_env(),
            )
        except Exception:
            self._process = None

    def _send(self, line: str) -> None:
        process = self._process
        if process and process.poll() is None:
            self._send_to_process(process, line)

    @staticmethod
    def _send_to_process(process: subprocess.Popen[str], line: str) -> None:
        if not process.stdin:
            return
        try:
            process.stdin.write(line.replace("\n", " ") + "\n")
            process.stdin.flush()
        except OSError:
            pass


def _splash_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, SPLASH_ARG]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"), SPLASH_ARG]


def splash_process_supported() -> bool:
    """Avoid launching a second copy of the bundled executable during startup."""

    return not getattr(sys, "frozen", False)


def _splash_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _iter_stdin_lines_utf8(stdin):
    stream = getattr(stdin, "buffer", None)
    if stream is not None:
        for raw in stream:
            yield raw.decode("utf-8", errors="replace").rstrip("\n")
        return
    for line in stdin:
        yield line.rstrip("\n")


def run_splash_process() -> int:
    """Run the splash window. Intended for the short-lived child process."""

    messages: queue.Queue[str] = queue.Queue()

    def read_stdin() -> None:
        try:
            for line in _iter_stdin_lines_utf8(sys.stdin):
                messages.put(line)
        finally:
            messages.put("CLOSE")

    threading.Thread(target=read_stdin, name="startup-splash-stdin", daemon=True).start()

    root: tk.Tk | None = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        root.configure(bg="#101216")

        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        width, height, x, y = _splash_layout(screen_w, screen_h)
        root.geometry(f"{width}x{height}+{x}+{y}")

        scale = min(width / SPLASH_PREFERRED_SIZE[0], height / SPLASH_PREFERRED_SIZE[1])
        horizontal_pad = max(6, round(28 * scale))
        title_y = min(max(height - 1, 1), max(1, round(44 * height / SPLASH_PREFERRED_SIZE[1])))
        status_y = min(
            max(height - 1, 1),
            max(title_y + max(2, round(18 * scale)), round(78 * height / SPLASH_PREFERRED_SIZE[1])),
        )
        track_top = min(
            max(height - 2, 0),
            max(status_y + max(2, round(8 * scale)), round(122 * height / SPLASH_PREFERRED_SIZE[1])),
        )
        track_bottom = min(
            height,
            max(track_top + 1, round(128 * height / SPLASH_PREFERRED_SIZE[1])),
        )
        footer_y = min(
            max(height - 1, 1),
            max(track_bottom + 1, round(148 * height / SPLASH_PREFERRED_SIZE[1])),
        )
        title_font_size = max(9, round(17 * scale))
        status_font_size = max(7, round(10 * scale))
        footer_font_size = max(7, round(9 * scale))

        canvas = tk.Canvas(
            root,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg="#101216",
        )
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(1, 1, width - 2, height - 2, outline="#2a323d", width=1)
        canvas.create_rectangle(0, 0, width, 4, fill="#3578f6", outline="")
        canvas.create_text(
            horizontal_pad,
            title_y,
            anchor="w",
            text="API 配置切换器",
            fill="#f3f6fb",
            font=("Microsoft YaHei UI", title_font_size, "bold"),
        )
        status_item = canvas.create_text(
            horizontal_pad,
            status_y,
            anchor="w",
            text="正在启动...",
            fill="#a0a9b5",
            font=("Microsoft YaHei UI", status_font_size),
        )
        track_right = max(horizontal_pad + 1, width - horizontal_pad)
        pulse_width = max(8, min(round(68 * scale), track_right - horizontal_pad))
        canvas.create_rectangle(horizontal_pad, track_top, track_right, track_bottom, fill="#20252d", outline="")
        pulse_item = canvas.create_rectangle(
            horizontal_pad,
            track_top,
            horizontal_pad + pulse_width,
            track_bottom,
            fill="#14a6a8",
            outline="",
        )
        canvas.create_text(
            width - horizontal_pad,
            footer_y,
            anchor="e",
            text="请稍候",
            fill="#737d8a",
            font=("Microsoft YaHei UI", footer_font_size),
        )

        state = {"offset": 0}

        def tick() -> None:
            try:
                while True:
                    line = messages.get_nowait()
                    if line == "CLOSE":
                        root.destroy()
                        return
                    if line.startswith("STATUS\t"):
                        message = line.split("\t", 1)[1].strip()
                        if message:
                            canvas.itemconfigure(status_item, text=message)
            except queue.Empty:
                pass

            track_left = horizontal_pad
            travel = max(track_right - track_left - pulse_width, 1)
            state["offset"] = (state["offset"] + 8) % travel
            x1 = track_left + state["offset"]
            canvas.coords(pulse_item, x1, track_top, x1 + pulse_width, track_bottom)
            root.after(33, tick)

        root.deiconify()
        root.lift()
        root.after(33, tick)
        root.mainloop()
    except Exception:
        return 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
    return 0
