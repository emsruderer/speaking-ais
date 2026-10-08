"""Monitor queue occupancy for the multi-process AIS application."""

import tkinter as tk
from tkinter import ttk

from queue_metrics import read


def queue_status_text(que, capacity, enqueued=None, dequeued=None):
    """Return queue depth and cumulative activity."""
    try:
        depth = f"{que.qsize()} / {capacity} queued (approximate)"
    except (NotImplementedError, OSError, ValueError):
        depth = "Queue size unavailable"
    return (
        f"{depth} | added: {read(enqueued)} | removed: {read(dequeued)}"
    )


class QueueMonitorWindow:
    """Display live occupancy for the raw-message and warning queues."""

    def __init__(self, root, queues, refresh_interval_ms=500, processes=()):
        self.root = root
        self.queues = queues
        self.processes = processes
        self.refresh_interval_ms = refresh_interval_ms
        self.status_vars = []
        self.progress_bars = []
        self.process_status = tk.StringVar()
        self.after_id = None

        root.title("AIS Queue Monitor")
        root.geometry("380x230")
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        for column, queue_info in enumerate(queues):
            name, _que, _capacity = queue_info[:3]
            queue_frame = ttk.Frame(frame, padding=6)
            queue_frame.grid(row=0, column=column, sticky="nsew")
            ttk.Label(
                queue_frame, text=name, wraplength=155, justify=tk.CENTER,
            ).pack(pady=(0, 5))
            status = tk.StringVar(value="Reading queue status...")
            self.status_vars.append(status)
            progress = ttk.Progressbar(
                queue_frame, orient=tk.VERTICAL, mode="determinate",
                length=100, maximum=_capacity,
            )
            progress.pack(pady=(0, 5))
            ttk.Label(
                queue_frame, textvariable=status, wraplength=155,
                justify=tk.CENTER,
            ).pack()
            self.progress_bars.append(progress)
            frame.columnconfigure(column, weight=1)
        ttk.Label(
            frame, textvariable=self.process_status, justify=tk.CENTER,
        ).grid(row=1, column=0, columnspan=max(1, len(queues)), sticky="ew",
               pady=(8, 0))

        self.refresh()

    def refresh(self):
        """Refresh queue labels and occupancy bars."""
        self.after_id = None
        for queue_info, status, progress in zip(
                self.queues, self.status_vars, self.progress_bars):
            _name, que, capacity, *counters = queue_info
            enqueued, dequeued = (counters + [None, None])[:2]
            status.set(queue_status_text(que, capacity, enqueued, dequeued))
            progress.configure(maximum=capacity)
            try:
                progress["value"] = que.qsize()
            except (NotImplementedError, OSError, ValueError):
                progress["value"] = 0

        self.process_status.set(
            "Workers: " + " | ".join(
                f"{name}: {'running' if process.is_alive() else 'stopped'}"
                for name, process in self.processes
            )
        )
        if self.root.winfo_exists():
            self.after_id = self.root.after(
                self.refresh_interval_ms, self.refresh,
            )

    def close(self):
        """Close the window and let the caller shut down the worker processes."""
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.root.destroy()
