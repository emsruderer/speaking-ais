"""Display vessel tracks produced by ais_stream."""

import argparse
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk

from ais_stream import add_mode_argument, apply_mode_argument, dangerous_track, do_track
from ships import Ship
from shipstype import shiptype

TRACK_COLUMNS = (
    "mmsi", "shipname", "callsign", "ship_type", "lat", "lon",
    "speed", "course", "cpa", "tcpa", "distance", "bearing",
)
DANGEROUS_COLUMNS = ("mmsi", "shipname", "cpa", "tcpa", "distance", "bearing")
DANGEROUS_VALUE_INDEXES = (0, 1, 8, 9, 10, 11)

def _format_number(value, suffix="", precision=2):
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{precision}f}{suffix}"

def _track_values(track):
    """Format one tracked vessel for the table."""
    if track.ship_type is None:
        ship_type = "—"
    elif 0 <= track.ship_type <= 99:
        ship_type = shiptype(track.ship_type)
    else:
        ship_type = f"Unknown type ({track.ship_type})"
    return (
        str(track.mmsi),
        track.shipname or "—",
        track.callsign or "—",
        ship_type,
        _format_number(track.lat, "°", 5),
        _format_number(track.lon, "°", 5),
        _format_number(track.speed, " kn"),
        _format_number(track.course, "°"),
        _format_number(track.cpa, " NM"),
        _format_number(track.tcpa, " min"),
        _format_number(track.distance, " NM"),
        _format_number(track.bearing, "°"),
    )


class AISMessageWindow:
    """Show the current tracks maintained by the ais_stream processing path."""

    def __init__(self, root, host="localhost", port=10110,
                 own_lat=53.264282, own_lon=7.397336, own_course=0.0,
                 own_speed=0.0, max_tracks=500):
        self.root = root
        self.host = host
        self.port = port
        self.max_tracks = max_tracks
        self.events = queue.Queue(maxsize=1000)
        self.stop_event = threading.Event()
        self.own_ship = Ship(
            244030153, own_lat, own_lon, own_course, own_speed,
            host=host, port=port,
        )

        root.title("AIS Vessel Tracks")
        root.geometry("1250x600")
        root.protocol("WM_DELETE_WINDOW", self.close)

        toolbar = ttk.Frame(root, padding=8)
        toolbar.pack(fill=tk.X)
        self.status = ttk.Label(
            toolbar,
            text=f"Waiting for AIS from {host}:{port} (automatic reconnect)",
            anchor=tk.W,
        )
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(toolbar, text="Clear", command=self.clear).pack(side=tk.RIGHT)

        panes = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        danger_pane = ttk.Frame(panes, padding=6)
        all_pane = ttk.Frame(panes, padding=6)
        panes.add(danger_pane, weight=1)
        panes.add(all_pane, weight=3)

        ttk.Label(
            danger_pane, text="Dangerous ships", font=("", 11, "bold"),
        ).pack(anchor=tk.W, pady=(0, 6))
        ttk.Label(
            all_pane, text="All tracked ships", font=("", 11, "bold"),
        ).pack(anchor=tk.W, pady=(0, 6))

        all_headings = {
            "mmsi": ("MMSI", 100),
            "shipname": ("Ship name", 180),
            "callsign": ("Call sign", 100),
            "ship_type": ("Ship type", 190),
            "lat": ("Latitude", 100),
            "lon": ("Longitude", 100),
            "speed": ("Speed", 80),
            "course": ("Course", 80),
            "cpa": ("CPA", 85),
            "tcpa": ("TCPA", 85),
            "distance": ("Range", 85),
            "bearing": ("Bearing", 85),
        }
        danger_headings = {
            "mmsi": ("MMSI", 100),
            "shipname": ("Ship name", 160),
            "cpa": ("CPA", 85),
            "tcpa": ("TCPA", 85),
            "distance": ("Range", 85),
            "bearing": ("Bearing", 85),
        }
        self.danger_table = self._create_table(
            danger_pane, DANGEROUS_COLUMNS, danger_headings,
        )
        self.table = self._create_table(all_pane, TRACK_COLUMNS, all_headings)

        self.own_ship.start()
        self.reader = threading.Thread(target=self._read_tracks, daemon=True)
        self.reader.start()
        self.root.after(100, self._show_events)

    @staticmethod
    def _create_table(parent, columns, headings):
        """Create a scrollable track table."""
        table_frame = ttk.Frame(parent)
        table_frame.pack(fill=tk.BOTH, expand=True)
        table = ttk.Treeview(table_frame, columns=columns, show="headings")
        for column, (title, width) in headings.items():
            table.heading(column, text=title)
            table.column(column, width=width, anchor=tk.W)

        y_scroll = ttk.Scrollbar(
            table_frame, orient=tk.VERTICAL, command=table.yview,
        )
        x_scroll = ttk.Scrollbar(
            table_frame, orient=tk.HORIZONTAL, command=table.xview,
        )
        table.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        table.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        return table

    def _enqueue(self, kind, track):
        """Queue a track snapshot for the Tk thread."""
        event = (
            kind,
            track.mmsi,
            _track_values(track),
            dangerous_track(track),
        )
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(event)

    def _read_tracks(self):
        while not self.stop_event.is_set():
            try:
                self.events.put(("reset", "", (), False))
                do_track(
                    None,
                    self.own_ship,
                    self.stop_event,
                    track_callback=self._enqueue,
                    host=self.host,
                    port=self.port,
                )
                if self.stop_event.is_set():
                    return
                self._queue_status("AIS stream ended; reconnecting in 5 seconds")
            except (OSError, ValueError) as error:
                if self.stop_event.is_set():
                    return
                self._queue_status(f"Stream error: {error}; retrying in 5 seconds")
            if self.stop_event.wait(5):
                return

    def _queue_status(self, text):
        try:
            self.events.put_nowait(("status", text, (), False))
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(("status", text, (), False))

    def _show_events(self):
        # Keep only the newest event per vessel, so a burst (such as the CPA
        # refresh of every track) costs one table update per vessel.
        latest = {}
        while True:
            try:
                kind, key, values, is_dangerous = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "status":
                self.status.configure(text=key)
                continue
            if kind == "reset":
                latest.clear()
                self._clear_table(self.table)
                self._clear_table(self.danger_table)
                continue
            latest.pop(key, None)  # re-insert so the newest event is applied last
            latest[key] = (kind, values, is_dangerous)

        if latest:
            self.status.configure(text=f"Receiving AIS from {self.host}:{self.port}")
        for key, (kind, values, is_dangerous) in latest.items():
            if kind == "delete":
                self._delete_track(key)
                continue
            item_id = str(key)
            self._upsert_track(self.table, item_id, values)
            if is_dangerous:
                danger_values = tuple(values[index] for index in DANGEROUS_VALUE_INDEXES)
                self._upsert_track(self.danger_table, item_id, danger_values)
            elif self.danger_table.exists(item_id):
                self.danger_table.delete(item_id)
        for table in (self.table, self.danger_table):
            for old_item in table.get_children()[self.max_tracks:]:
                table.delete(old_item)

        if not self.stop_event.is_set():
            self.root.after(100, self._show_events)

    def clear(self):
        # Clear both the main and danger tables and notify the event loop to reset
        self._clear_table(self.table)
        self._clear_table(self.danger_table)

    @staticmethod
    def _clear_table(table):
        for item in table.get_children():
            table.delete(item)

    def _upsert_track(self, table, item_id, values):
        if table.exists(item_id):
            table.item(item_id, values=values)
            table.move(item_id, "", 0)
        else:
            table.insert("", 0, iid=item_id, values=values)

    def _delete_track(self, mmsi):
        item_id = str(mmsi)
        for table in (self.table, self.danger_table):
            if table.exists(item_id):
                table.delete(item_id)

    def close(self):
        self.stop_event.set()
        self.own_ship.stop()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost",
                        help="SignalK AIS TCP host (default: localhost)")
    parser.add_argument("--port", type=int, default=10110,
                        help="SignalK AIS TCP port (default: 10110)")
    parser.add_argument("--own-lat", type=float, default=53.264282,
                        help="own-vessel latitude in decimal degrees")
    parser.add_argument("--own-lon", type=float, default=7.397336,
                        help="own-vessel longitude in decimal degrees")
    parser.add_argument("--own-course", type=float, default=0.0,
                        help="own-vessel course over ground in degrees")
    parser.add_argument("--own-speed", type=float, default=0.0,
                        help="own-vessel speed over ground in knots")
    add_mode_argument(parser)
    args = parser.parse_args()
    apply_mode_argument(args)

    root = tk.Tk()
    AISMessageWindow(
        root,
        host=args.host,
        port=args.port,
        own_lat=args.own_lat,
        own_lon=args.own_lon,
        own_course=args.own_course,
        own_speed=args.own_speed,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
