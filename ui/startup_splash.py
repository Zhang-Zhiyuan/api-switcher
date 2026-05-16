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


def run_splash_process() -> int:
    """Run the splash window. Intended for the short-lived child process."""

    messages: queue.Queue[str] = queue.Queue()

    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                messages.put(line.rstrip("\n"))
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

        width, height = 380, 168
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")

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
            28,
            44,
            anchor="w",
            text="API 配置切换器",
            fill="#f3f6fb",
            font=("Microsoft YaHei UI", 17, "bold"),
        )
        status_item = canvas.create_text(
            28,
            78,
            anchor="w",
            text="正在启动...",
            fill="#a0a9b5",
            font=("Microsoft YaHei UI", 10),
        )
        canvas.create_rectangle(28, 122, width - 28, 128, fill="#20252d", outline="")
        pulse_item = canvas.create_rectangle(28, 122, 96, 128, fill="#14a6a8", outline="")
        canvas.create_text(
            width - 28,
            148,
            anchor="e",
            text="请稍候",
            fill="#737d8a",
            font=("Microsoft YaHei UI", 9),
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

            track_left, track_right = 28, 352
            pulse_width = 76
            travel = max(track_right - track_left - pulse_width, 1)
            state["offset"] = (state["offset"] + 8) % travel
            x1 = track_left + state["offset"]
            canvas.coords(pulse_item, x1, 122, x1 + pulse_width, 128)
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
