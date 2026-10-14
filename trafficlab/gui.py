"""trafficlab control panel - a simple window with a START button.

Press START BOTS and the selected number of browser visitors begin browsing
your site: different pages, full page loads (ads, images, video all execute),
at the rate and page-wait time you choose. STOP ends the run cleanly and still
prints the summary.

The runner executes in a background thread with its own asyncio loop; its
output is piped into the log box through a thread-safe queue.
"""

from __future__ import annotations

import asyncio
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import runner
from .config import Settings

MODE_CHOICES = ["Real browser bots", "Header-only HTTP bots", "Mixed (both)"]
MODE_MAP = {
    "Real browser bots": "browser",
    "Header-only HTTP bots": "http",
    "Mixed (both)": "mixed",
}
_LIVE_LINE = re.compile(r"\[\s*\d+\.\d+s\] sess=")


class _LogWriter:
    """Collects prints from the runner thread as complete lines."""

    def __init__(self) -> None:
        self._buf = ""
        self.lines: queue.Queue[str] = queue.Queue()

    def write(self, text: str) -> int:
        self._buf += text.replace("\r", "")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.lines.put(line)
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            self.lines.put(self._buf)
        self._buf = ""


class _RunThread(threading.Thread):
    def __init__(self, settings: Settings, writer: _LogWriter) -> None:
        super().__init__(daemon=True, name="trafficlab-run")
        self.settings = settings
        self.writer = writer
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.exit_code: int | None = None

    def run(self) -> None:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = self.writer
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.task = self.loop.create_task(runner.run(self.settings))
            self.exit_code = self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            self.exit_code = 130
            self.writer.write("stopped.\n")
        except Exception as exc:                       # pragma: no cover
            self.exit_code = -1
            self.writer.write(f"error: {type(exc).__name__}: {exc}\n")
        finally:
            try:
                if self.loop is not None:
                    self.loop.close()
            except Exception:
                pass
            sys.stdout, sys.stderr = old_out, old_err

    def stop(self) -> None:
        if self.loop is not None and self.loop.is_running() and self.task is not None:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass


class App:
    def __init__(self, root: tk.Tk, prefill_url: str | None = None) -> None:
        self.root = root
        self.writer = _LogWriter()
        self.thread: _RunThread | None = None
        self._closing = False
        self._destroyed = False

        root.title("trafficlab - bot traffic control panel")
        root.geometry("900x680")
        root.minsize(760, 560)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- state ----
        self.url = tk.StringVar(value=prefill_url or "https://example.com")
        self.mode = tk.StringVar(value=MODE_CHOICES[0])
        self.workers = tk.StringVar(value="10")
        self.rate = tk.StringVar(value="1.0")
        self.wait = tk.StringVar(value="4")
        self.duration = tk.StringVar(value="60")
        self.pages = tk.StringVar(value="")
        self.pages_per_session = tk.StringVar(value="3")
        self.show_windows = tk.BooleanVar(value=False)
        self.full_load = tk.BooleanVar(value=True)
        self.real_chrome = tk.BooleanVar(value=False)
        self.stealth = tk.BooleanVar(value=False)
        self.returning = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="idle - set your options, then press START BOTS")

        self._build()
        self._poll()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 4}
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="trafficlab control panel",
                  font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(header, text="only for sites you own",
                  foreground="#777").pack(side="right")

        # ---- settings grid ----
        box = ttk.LabelFrame(outer, text="Bot settings", padding=8)
        box.pack(fill="x", pady=(10, 6))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Your site URL").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.url).grid(row=0, column=1, columnspan=5,
                                                   sticky="ew", **pad)

        ttk.Label(box, text="Bot kind").grid(row=1, column=0, sticky="w", **pad)
        ttk.Combobox(box, textvariable=self.mode, values=MODE_CHOICES,
                     state="readonly", width=22).grid(row=1, column=1,
                                                      sticky="w", **pad)

        ttk.Label(box, text="Browsers in parallel").grid(row=1, column=2,
                                                         sticky="e", **pad)
        ttk.Spinbox(box, from_=1, to=50, textvariable=self.workers,
                    width=5).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(box, text="Rate (sessions/sec)").grid(row=2, column=0,
                                                       sticky="w", **pad)
        ttk.Spinbox(box, from_=0.1, to=50, increment=0.5, textvariable=self.rate,
                    width=6).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(box, text="Wait per page (sec)").grid(row=2, column=2,
                                                        sticky="e", **pad)
        ttk.Spinbox(box, from_=0, to=120, increment=1, textvariable=self.wait,
                    width=6).grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(box, text="Run for (sec)").grid(row=3, column=0, sticky="w", **pad)
        ttk.Spinbox(box, from_=5, to=3600, increment=10, textvariable=self.duration,
                    width=6).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(box, text="Pages per session").grid(row=3, column=2,
                                                      sticky="e", **pad)
        ttk.Spinbox(box, from_=1, to=12, textvariable=self.pages_per_session,
                    width=6).grid(row=3, column=3, sticky="w", **pad)

        ttk.Label(box, text="Pages (blank = all)").grid(row=4, column=0,
                                                        sticky="w", **pad)
        ttk.Entry(box, textvariable=self.pages).grid(row=4, column=1, columnspan=5,
                                                     sticky="ew", **pad)

        checks = ttk.Frame(box)
        checks.grid(row=5, column=0, columnspan=6, sticky="w", **pad)
        ttk.Checkbutton(checks, text="Full page load (ads, images, video all execute)",
                        variable=self.full_load).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(checks, text="Returning visitors (remember cookies)",
                        variable=self.returning).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(checks, text="Real Chrome",
                        variable=self.real_chrome).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(checks, text="Show browser windows",
                        variable=self.show_windows).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(checks, text="Stealth (localhost mirror only)",
                        variable=self.stealth).pack(side="left")

        # ---- buttons ----
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(2, 6))
        self.start_btn = tk.Button(buttons, text="START BOTS", command=self.start,
                                   bg="#1f8f4e", fg="white", activebackground="#28a860",
                                   activeforeground="white", font=("Segoe UI", 12, "bold"),
                                   width=16, height=1, relief="flat", cursor="hand2")
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = tk.Button(buttons, text="STOP", command=self.stop,
                                  bg="#b3392f", fg="white", activebackground="#d04a3e",
                                  activeforeground="white", font=("Segoe UI", 12, "bold"),
                                  width=10, height=1, relief="flat", state="disabled",
                                  cursor="hand2")
        self.stop_btn.pack(side="left")
        ttk.Label(buttons, text="summary + raw data land in results/",
                  foreground="#777").pack(side="right")

        # ---- log ----
        log_box = ttk.LabelFrame(outer, text="Live log", padding=4)
        log_box.pack(fill="both", expand=True)
        self.log = tk.Text(log_box, height=16, state="disabled", wrap="word",
                           background="#111", foreground="#d8d8d8",
                           insertbackground="white", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_box, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("btn", foreground="#7ad07a")

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(6, 0))
        ttk.Label(status, textvariable=self.status, anchor="w").pack(fill="x")

    # ------------------------------------------------------------- actions
    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        url = self.url.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showerror("Missing URL", "Enter a full URL, e.g. https://your-site.dev")
            return
        try:
            rate = float(self.rate.get())
            wait = float(self.wait.get())
            duration = float(self.duration.get())
            workers = int(self.workers.get())
            pps = int(self.pages_per_session.get())
            if pps < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Bad number",
                                 "Rate, wait, run time, browsers and pages per session "
                                 "must be whole numbers.")
            return

        mode = MODE_MAP[self.mode.get()]
        if mode == "mixed" and workers < 2:
            workers = 2
        pages = [p.strip() for p in self.pages.get().split(",") if p.strip()]

        settings = Settings(
            url=url,
            mode=mode,
            rate=rate,
            visits=None,
            duration=duration,
            concurrency=workers,
            browser_ratio=0.6,
            pages=pages,
            dwell_min=wait,
            dwell_max=wait + 1.0,          # a little natural variation
            pages_per_session=pps,
            persist_sessions=bool(self.returning.get()),
            channel="chrome" if self.real_chrome.get() else "",
            stealth=bool(self.stealth.get()),
            full_load=bool(self.full_load.get()),
            headless=not self.show_windows.get(),
            yes=True,                      # the button press IS the confirmation
        )
        try:
            settings.validate()
        except ValueError as exc:
            messagebox.showerror("Settings", str(exc))
            return

        self._append("-" * 56, "btn")
        self._append(f"START: {mode} bots, {rate:g} sessions/s for {duration:g}s, "
                     f"{workers} in parallel, {pps} pages/session, wait {wait:g}s per page, "
                     f"full load {'on' if settings.full_load else 'off'}"
                     + (", returning visitors" if settings.persist_sessions else "")
                     + (", stealth (loopback only)" if settings.stealth else ""), "btn")
        if mode in ("browser", "mixed") and settings.full_load:
            browser_workers = workers if mode == "browser" else max(1, round(workers * 0.6))
            session_seconds = 9.0 * pps
            capacity = browser_workers / session_seconds
            demand = rate if mode == "browser" else rate * 0.6
            if demand > capacity:
                self._append(f"note: ~9s/page x {pps} pages = ~{session_seconds:.0f}s/session; "
                             f"{browser_workers} browser workers sustain ~{capacity:.1f} "
                             f"sessions/s (~{capacity * pps:.1f} page views/s). To reach "
                             f"{demand:.1f}/s use ~{int(demand * session_seconds) + 1} "
                             f"parallel browsers or a lower rate.", "btn")
        self._append("-" * 56, "btn")

        self.thread = _RunThread(settings, self.writer)
        self.thread.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.set("running...")

    def stop(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.thread.stop()
            self.stop_btn.config(state="disabled")
            self.status.set("stopping - finishing in-flight visits...")

    # ------------------------------------------------------------- plumbing
    def _append(self, line: str, tag: str | None = None) -> None:
        self.log.config(state="normal")
        self.log.insert("end", line + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _poll(self) -> None:
        if self._destroyed:
            return
        try:
            drained = False
            while True:
                try:
                    line = self.writer.lines.get_nowait()
                except queue.Empty:
                    break
                drained = True
                if _LIVE_LINE.search(line):
                    self.status.set(line.strip())
                self._append(line)

            running = self.thread is not None and self.thread.is_alive()
            if running:
                self.start_btn.config(state="disabled")
                self.stop_btn.config(state="normal")
            else:
                self.stop_btn.config(state="disabled")
                if self.thread is not None and self.start_btn["state"] == "disabled":
                    code = self.thread.exit_code
                    label = "stopped" if code == 130 else f"finished (code {code})"
                    self.start_btn.config(state="normal")
                    self.status.set(f"idle - last run {label}")
                    self.thread = None

            if self._closing and self.thread is None:
                self._destroyed = True
                self.root.destroy()
                return
            self.root.after(120, self._poll)
        except tk.TclError:                       # window went away mid-callback
            self._destroyed = True

    def _on_close(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            if not messagebox.askyesno("Bots running",
                                       "Bots are still running. Stop them and exit?"):
                return
            self.thread.stop()
            self._closing = True      # _poll destroys the window once it ends
            self.status.set("stopping before exit...")
            return
        self.root.destroy()


def main(prefill_url: str | None = None) -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:                      # pragma: no cover
        print(f"could not open a window: {exc}", file=sys.stderr)
        return 1
    App(root, prefill_url=prefill_url)
    root.mainloop()
    return 0
