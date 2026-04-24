"""Desktop launcher for the Aerial Object Detection server.

Double-click this file (Windows binds .pyw to pythonw.exe) to open a small
tkinter window with Start / Stop / Dashboard buttons. The server runs as a
child process; Stop sends CTRL_BREAK_EVENT so the server's KeyboardInterrupt
handler runs and in-flight MP4 clips finalize cleanly.
"""

from __future__ import annotations

import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent
MAX_LOG_LINES = 500
READY_TIMEOUT_S = 30
STOP_TIMEOUT_S = 15

STATE_STOPPED = "Stopped"
STATE_STARTING = "Starting…"
STATE_RUNNING = "Running"
STATE_STOPPING = "Stopping…"
STATE_CRASHED = "Crashed"

STATE_COLOR = {
    STATE_STOPPED: "#888888",
    STATE_STARTING: "#d79a00",
    STATE_RUNNING: "#2aa745",
    STATE_STOPPING: "#d79a00",
    STATE_CRASHED: "#c0392b",
}


def _attach_hidden_console_on_windows() -> None:
    # pythonw.exe has no console, so a child spawned from us also has no
    # console and CTRL_BREAK_EVENT cannot be delivered. Allocate one for
    # ourselves and hide it; the child inherits it and CTRL_BREAK works.
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if kernel32.GetConsoleWindow() == 0:
        if kernel32.AllocConsole():
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                user32.ShowWindow(hwnd, 0)  # SW_HIDE


class LauncherApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Aerial Object Detection — Launcher")
        self.geometry("620x440")
        self.minsize(520, 360)

        self.proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.state: str = STATE_STOPPED
        self._ready_deadline: float | None = None

        self._build_ui()
        self._set_state(STATE_STOPPED)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_log)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Status:").pack(side=tk.LEFT)
        self.status_dot = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(6, 4))
        self._dot_item = self.status_dot.create_oval(
            2, 2, 12, 12, fill=STATE_COLOR[STATE_STOPPED], outline=""
        )
        self.status_label = ttk.Label(top, text=STATE_STOPPED, width=12)
        self.status_label.pack(side=tk.LEFT)

        controls = ttk.Frame(self, padding=(8, 0, 8, 8))
        controls.pack(fill=tk.X)

        ttk.Label(controls, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="8080")
        self.port_entry = ttk.Entry(controls, textvariable=self.port_var, width=7)
        self.port_entry.pack(side=tk.LEFT, padx=(4, 12))

        self.start_btn = ttk.Button(controls, text="Start", command=self._on_start)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        self.stop_btn = ttk.Button(controls, text="Stop", command=self._on_stop)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        self.dash_btn = ttk.Button(
            controls, text="Open Dashboard", command=self._on_dashboard
        )
        self.dash_btn.pack(side=tk.LEFT, padx=2)

        log_frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(
            log_frame, wrap="none", state="disabled", height=18,
            bg="#1e1e1e", fg="#dcdcdc", insertbackground="#dcdcdc",
            font=("Consolas", 9),
        )
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text["yscrollcommand"] = scroll.set
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _set_state(self, new_state: str) -> None:
        self.state = new_state
        self.status_label.config(text=new_state)
        self.status_dot.itemconfig(self._dot_item, fill=STATE_COLOR[new_state])
        can_start = new_state in (STATE_STOPPED, STATE_CRASHED)
        can_stop = new_state in (STATE_RUNNING, STATE_STARTING)
        can_open = new_state == STATE_RUNNING
        self.start_btn.state(["!disabled"] if can_start else ["disabled"])
        self.stop_btn.state(["!disabled"] if can_stop else ["disabled"])
        self.dash_btn.state(["!disabled"] if can_open else ["disabled"])
        self.port_entry.state(["!disabled"] if can_start else ["disabled"])

    def _append_log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line if line.endswith("\n") else line + "\n")
        # Trim oldest lines if over cap.
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES + 1}.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _drain_log(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def _on_start(self) -> None:
        port_str = self.port_var.get().strip()
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid port", f"'{port_str}' is not a valid port.")
            return
        self.port = port

        argv = [sys.executable, "-m", "src.main", "-v"]
        if port != 8080:
            argv += ["--port", str(port)]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as exc:
            messagebox.showerror("Start failed", f"Could not start server:\n{exc}")
            return

        self._append_log(f"$ {' '.join(argv)}")
        threading.Thread(target=self._pump_output, args=(self.proc,), daemon=True).start()

        self._set_state(STATE_STARTING)
        import time as _t
        self._ready_deadline = _t.monotonic() + READY_TIMEOUT_S
        self.after(500, self._poll_ready)

    def _pump_output(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(line.rstrip("\n"))
        # stdout closed — process is exiting or already gone.

    def _poll_ready(self) -> None:
        import time as _t

        if self.proc is None:
            return
        if self.state not in (STATE_STARTING,):
            return

        if self.proc.poll() is not None:
            # Child died during startup.
            self.log_queue.put(
                f"[launcher] server exited during startup (code={self.proc.returncode})"
            )
            self._set_state(STATE_CRASHED)
            return

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/", timeout=0.5
            ) as resp:
                if 200 <= resp.status < 500:
                    self.log_queue.put("[launcher] server is ready")
                    self._set_state(STATE_RUNNING)
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass

        if self._ready_deadline and _t.monotonic() > self._ready_deadline:
            self.log_queue.put(
                f"[launcher] timed out waiting for http://127.0.0.1:{self.port}/"
            )
            self._set_state(STATE_CRASHED)
            return

        self.after(500, self._poll_ready)

    def _on_stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self._set_state(STATE_STOPPED)
            return
        self._set_state(STATE_STOPPING)
        self.log_queue.put("[launcher] sending shutdown signal")
        try:
            if sys.platform == "win32":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGINT)
        except OSError as exc:
            self.log_queue.put(f"[launcher] send_signal failed: {exc}")
        threading.Thread(target=self._await_stop, args=(self.proc,), daemon=True).start()

    def _await_stop(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
            self.log_queue.put(f"[launcher] server exited (code={proc.returncode})")
            self.after(0, lambda: self._set_state(STATE_STOPPED))
            return
        except subprocess.TimeoutExpired:
            pass

        self.log_queue.put(
            "[launcher] WARNING: graceful shutdown timed out — forcing kill. "
            "In-flight MP4 clips may be corrupt."
        )
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self.log_queue.put(f"[launcher] server force-killed (code={proc.returncode})")
        self.after(0, lambda: self._set_state(STATE_STOPPED))

    def _on_dashboard(self) -> None:
        port = getattr(self, "port", 8080)
        webbrowser.open(f"http://localhost:{port}/")

    def _on_close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno(
                "Server still running",
                "The server is still running. Stop it and exit?",
            ):
                return
            self._on_stop()
            # Give the stop thread a moment to finalize before destroying.
            self.after(500, self._check_close)
            return
        self.destroy()

    def _check_close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.after(200, self._check_close)
            return
        self.destroy()


def main() -> None:
    _attach_hidden_console_on_windows()
    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
