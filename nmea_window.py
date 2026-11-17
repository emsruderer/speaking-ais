#!/home/nanno/.pyenv/shims/python3
"""Live NMEA 0183 monitor: one field per sentence ($GLGSV, $GNRMC, !AIVDM, ...).

Reads SignalK's NMEA 0183 TCP stream (settings.HOST:settings.PORT) and shows
the decoded content of every sentence identifier in its own field. GSV fields
show the full satellite table per signal (SNR bar, quality, GLONASS slot,
"used in fix" from GSA), assembled from the multipart cycle.

    python nmea_window.py [--host H] [--port P] [--columns N]
"""
import argparse
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import settings
from gps_stream import NmeaFieldDecoder, JammingMonitor, TALKERS, \
    TALKER_SYSTEM_ID, SYSTEM_IDS, PAIRSPF_STATUS, fmt_ais, fmt_pos, fmt_xdr

DESCRIPTIONS = {
    "GSV": "satellites in view", "GSA": "DOP and active satellites",
    "RMC": "recommended minimum data", "VTG": "course and speed over ground",
    "GGA": "fix data", "GLL": "geographic position", "ZDA": "time and date",
    "HDT": "heading true", "HDM": "heading magnetic",
    "HDG": "heading, deviation and variation", "XDR": "transducer measurements",
    "VDM": "AIS, other vessels", "VDO": "AIS, own vessel",
    "TXT": "text message", "GNS": "fix data", "GST": "position error statistics",
    "PAIRSPF": "receiver jamming status, L1", "PAIRSPF5": "receiver jamming status, L5",
    "PAIR001": "receiver command acknowledgement",
}
STALE_S = 5                       # field title turns grey after this
BAR_WIDTH = 20                    # characters for SNR 0..50 dB
# Dark theme
BG = "#0d1117"                    # window background
BG_PANEL = "#161b22"              # text areas
FG = "#e6edf3"                    # normal text
FG_DIM = "#8b949e"                # secondary text
FG_STALE = "#484f58"              # title of a field without recent data
BORDER = "#30363d"
TAG_COLORS = {"q_exc": "#3fb950", "q_good": "#8fd16a", "q_weak": "#d29922",
              "q_vweak": "#f85149", "q_none": "#6e7681", "err": "#f85149",
              "dim": FG_DIM, "used": "#58a6ff"}
LEVEL_COLORS = {"learning": FG_DIM, "ok": "#3fb950", "watch": "#d29922",
                "warning": "#f0883e", "alarm": "#f85149", "dim": FG_DIM}
JAM_COLORS = {"learning": ("#30363d", FG), "ok": ("#1a7f37", "white"),
              "watch": ("#9e6a03", "white"), "warning": ("#bd561d", "white"),
              "alarm": ("#b62324", "white")}
QUALITY_TAG = {"excellent": "q_exc", "good": "q_good", "weak": "q_weak",
               "very weak": "q_vweak", "not tracked": "q_none"}


# ---------------------------------------------------------------- reader thread

def reader(host, port, out, stop):
    """Put ("line", text) and ("status", text) tuples on the queue."""
    while not stop.is_set():
        try:
            with socket.create_connection((host, port), timeout=10) as s:
                s.settimeout(30)
                out.put(("status", f"connected to {host}:{port}"))
                buf = b""
                while not stop.is_set():
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        if len(raw) <= 4096:
                            out.put(("line", raw.decode("ascii", "replace").strip()))
                    if len(buf) > 4096:
                        buf = b""
                out.put(("status", "connection closed, reconnecting"))
        except OSError as e:
            out.put(("status", f"connection problem: {e}; retry in 5 s"))
        out.put(("reset", None))
        stop.wait(5)


# ---------------------------------------------------------------- formatting

def fmt_time(t):
    if not t or len(t) < 6:
        return t or "n/a"
    return f"{t[0:2]}:{t[2:4]}:{t[4:]} UTC"


def fmt_value(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def snr_bar(snr):
    if snr is None:
        return "·" * BAR_WIDTH
    n = max(0, min(BAR_WIDTH, round(snr / 50 * BAR_WIDTH)))
    return "█" * n + "·" * (BAR_WIDTH - n)


# ---------------------------------------------------------------- one field

class Field:
    def __init__(self, app, parent, key, kind, talker):
        self.app, self.key, self.kind, self.talker = app, key, kind, talker
        self.parts = {}              # sub-key -> data (GSV per signal, GSA per system)
        self.count = 0
        self.last = 0.0
        self.raw = ""
        self.error = None
        self.dirty = False

        system = TALKERS.get(talker, "")
        if kind in ("VDM", "VDO"):
            system = ""
        elif talker == "AP":
            system = "autopilot"
        elif talker == "II":
            system = "integrated instrumentation"
        title = f"{key}   " + " · ".join(
            x for x in (system, DESCRIPTIONS.get(kind, "")) if x)
        self.frame = tk.LabelFrame(parent, text=title, padx=6, pady=4,
                                   font=app.title_font, fg=app.fg_title)
        self.info = tk.Label(self.frame, anchor="w", font=app.small_font, fg=FG_DIM)
        self.info.pack(fill="x")
        width = app.ais_width if kind in ("VDM", "VDO") else app.text_width
        self.text = tk.Text(self.frame, height=2, width=width, wrap="none",
                            font=app.mono, relief="flat", bg=app.bg_text,
                            borderwidth=0, highlightthickness=0)
        self.text.pack(fill="both", expand=True)
        for tag, color in TAG_COLORS.items():
            self.text.tag_configure(tag, foreground=color)
        self.text.tag_configure("head", font=app.mono_bold)
        self.text.configure(state="disabled")
        self.rawlabel = tk.Label(self.frame, anchor="w", font=app.small_mono,
                                 fg=FG_DIM, justify="left")
        self.rawlabel.pack(fill="x")

    def update(self, msg):
        self.count += 1
        self.last = time.monotonic()
        self.raw = msg["raw"]
        self.error = msg["error"]
        d = msg["data"]
        if d is not None:
            if self.kind == "GSV":
                self.parts[d["signal_id"] or ""] = d
            elif self.kind == "GSA":
                self.parts[d["system_id"] or d["talker"]] = d
            elif self.kind == "XDR" and "measurements" in d:
                # pypilot sends pitch and roll in separate sentences: keep each
                for m in d["measurements"]:
                    self.parts[m["name"] or m["type"]] = {"measurements": [m]}
            else:
                self.parts[""] = d
        self.dirty = True

    def tick(self, now):
        age = now - self.last
        self.info.configure(text=f"{self.count} sentences · last {age:.0f} s ago")
        self.frame.configure(fg=self.app.fg_title if age < STALE_S else FG_STALE)

    # -- rendering: list of (text, tag) chunks
    def render(self):
        self.dirty = False
        chunks = []
        if self.error:
            chunks.append((f"Malformed: {self.error}\n", "err"))
        if not self.parts:
            chunks.append(("waiting for complete message…\n", "dim"))
        elif self.kind == "GSV":
            self.render_gsv(chunks)
        elif self.kind == "GSA":
            self.render_gsa(chunks)
        elif self.kind == "XDR" and "" not in self.parts:
            for name in sorted(self.parts):
                for line in fmt_xdr(self.parts[name]):
                    chunks.append((f"  {line}\n", None))
        elif self.kind in ("VDM", "VDO"):
            d = self.parts[""]
            chunks.append((fmt_ais(d, d.get("channel", "?")) + "\n", None))
        else:
            self.render_generic(chunks, self.parts[""])

        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for s, tag in chunks:
            t.insert("end", s, tag) if tag else t.insert("end", s)
        t.delete("end-2c", "end-1c")                 # trailing newline
        lines = int(t.index("end-1c").split(".")[0])
        t.configure(height=max(1, min(lines, 45)), state="disabled")
        self.rawlabel.configure(text=self.raw[:110] + ("…" if len(self.raw) > 110 else ""))

    def render_gsv(self, chunks):
        used = self.app.used.get(TALKER_SYSTEM_ID.get(self.talker), set())
        glonass = self.talker == "GL"
        for sig in sorted(self.parts):
            d = self.parts[sig]
            summary = (f"Signal {d['signal_id'] or '-'} · {d['signal']}: "
                       f"{d['in_view']} in view, {d['tracked']} tracked")
            if d["mean_snr"] is not None:
                summary += f", mean SNR {d['mean_snr']:.0f} dB, max {d['max_snr']} dB"
            chunks.append((summary + "\n", "head"))
            hdr = f"  {'PRN':>4}" + (f" {'Slot':>4}" if glonass else "") + \
                f" {'Elev':>5} {'Azimuth':>9} {'SNR':>6} Fix  {'Signal':<{BAR_WIDTH}} Quality\n"
            chunks.append((hdr, "dim"))
            sats = sorted(d["satellites"],
                          key=lambda s: (s["snr"] is None, -(s["snr"] or 0)))
            for s in sats:
                el = "--" if s["el"] is None else f"{s['el']}°"
                az = "--" if s["az"] is None else f"{s['az']:3d}° {s['direction']:<3}"
                snr = "--" if s["snr"] is None else f"{s['snr']} dB"
                slot = ""
                if glonass:
                    slot = f" {s['slot'] if s.get('slot') else '-':>4}"
                is_used = s["prn"].lstrip("0") in used
                tag = QUALITY_TAG[s["quality"]]
                chunks.append((f"  {s['prn']:>4}{slot} {el:>5} {az:>9} {snr:>6} ", None))
                chunks.append(("  ✓  " if is_used else "     ", "used"))
                chunks.append((snr_bar(s["snr"]) + " ", tag))
                chunks.append((s["quality"], tag))
                if s.get("note"):
                    chunks.append((f"  ({s['note']})", "q_weak"))
                chunks.append(("\n", None))
            for note in d["notes"]:
                chunks.append((f"  Note: {note}\n", "q_weak"))

    def render_gsa(self, chunks):
        first = True
        for sysid in sorted(self.parts):
            d = self.parts[sysid]
            if first:
                chunks.append((f"Mode {d['mode']} · {d['fix']} · "
                               f"PDOP {fmt_value(d['pdop'])}  HDOP {fmt_value(d['hdop'])}  "
                               f"VDOP {fmt_value(d['vdop'])}\n", "head"))
                first = False
            name = SYSTEM_IDS.get(d["system_id"], d["system"]) if d["system_id"] \
                else d["system"]
            used = ", ".join(d["used"]) or "none"
            chunks.append((f"  {name:<8} {len(d['used']):>2} used: {used}\n", None))
            if d["fix_code"] == "1":
                chunks.append(("  Warning: no fix, position is not valid.\n", "err"))

    def render_generic(self, chunks, d):
        if "fields" in d:
            for i, v in enumerate(d["fields"], 1):
                chunks.append((f"  field {i:<2} {v or '(empty)'}\n", None))
            return
        if "latitude" in d:
            chunks.append((f"  {'position':<20}{fmt_pos(d['latitude'], d['longitude'])}\n",
                           None))
        for k, v in d.items():
            if k in ("sentence", "latitude", "longitude"):
                continue
            if k == "time_utc":
                v = fmt_time(v)
            elif isinstance(v, (int, float)) and k.endswith(
                    ("heading", "deviation", "variation")):
                if k.endswith("heading") or v == 0:
                    v = f"{v:.1f}°"
                else:
                    v = f"{abs(v):.1f}° " + ("E" if v > 0 else "W")
            chunks.append((f"  {k.replace('_', ' '):<20}{fmt_value(v)}\n", None))


# ---------------------------------------------------------------- window

class App:
    def __init__(self, root, host, port, columns):
        self.root, self.columns = root, columns
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.decoder = NmeaFieldDecoder()
        self.fields = {}
        self.used = {}                       # GSA system id -> set of PRNs used
        self.lines = 0
        self.bad = 0

        self.mono = tkfont.Font(family="DejaVu Sans Mono", size=9)
        self.mono_bold = tkfont.Font(family="DejaVu Sans Mono", size=9, weight="bold")
        self.small_mono = tkfont.Font(family="DejaVu Sans Mono", size=8)
        self.small_font = tkfont.Font(size=8)
        self.title_font = tkfont.Font(size=10, weight="bold")
        self.fg_title, self.bg_text = FG, BG_PANEL
        self.apply_dark_theme(root)
        self.text_width = 104
        self.ais_width = 80                  # AIS column: fmt_ais lines are up to ~72

        root.title("NMEA 0183 monitor")
        self.status = tk.Label(root, anchor="w", padx=8, relief="sunken")
        self.status.pack(side="bottom", fill="x")
        self.build_jamming_panel(root)

        self.canvas = tk.Canvas(root, highlightthickness=0)
        sb = tk.Scrollbar(root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        # GNSS and other sentences flow over --columns columns; AIS gets its
        # own column on the right, stacked independently of the grid rows.
        self.grid_area = tk.Frame(self.inner)
        self.grid_area.grid(row=0, column=0, sticky="n")
        self.ais_area = tk.Frame(self.inner)
        self.ais_area.grid(row=0, column=1, sticky="n")
        self.n_grid = 0
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        root.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        root.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        self.conn = "connecting…"
        self.jam = JammingMonitor()
        self.jam_level = "learning"

        threading.Thread(target=reader, args=(host, port, self.queue, self.stop),
                         daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        root.after(1000, self.tick)

    @staticmethod
    def apply_dark_theme(root):
        """Default colours for every widget created after this call."""
        root.configure(bg=BG)
        for pattern, value in (
                ("*Background", BG), ("*Foreground", FG),
                ("*activeBackground", BORDER), ("*activeForeground", FG),
                ("*highlightBackground", BG), ("*highlightColor", BORDER),
                ("*troughColor", BG_PANEL), ("*insertBackground", FG),
                ("*selectBackground", "#264f78"), ("*selectForeground", FG),
                ("*Text.Background", BG_PANEL), ("*Button.Background", BG_PANEL),
                ("*Scrollbar.Background", BORDER)):
            root.option_add(pattern, value)

    def build_jamming_panel(self, root):
        self.banner = tk.Label(root, anchor="w", padx=10, pady=6,
                               font=tkfont.Font(size=12, weight="bold"))
        self.banner.pack(side="top", fill="x")
        box = tk.LabelFrame(root, text="GNSS interference / jamming monitor",
                            padx=6, pady=4, font=self.title_font)
        box.pack(side="top", fill="x", padx=4, pady=(2, 4))
        tk.Button(box, text="Re-learn baseline", font=self.small_font,
                  command=self.reset_jamming).pack(side="right", anchor="n")
        self.jam_text = tk.Text(box, height=4, wrap="none", font=self.mono,
                                relief="flat", bg=self.bg_text, borderwidth=0,
                                highlightthickness=0)
        self.jam_text.pack(side="left", fill="x", expand=True)
        for tag, color in LEVEL_COLORS.items():
            self.jam_text.tag_configure(tag, foreground=color)
        self.jam_text.configure(state="disabled")

    def reset_jamming(self):
        self.jam.reset()
        self.jam_level = "learning"

    def show_jamming(self, r):
        level = r["level"]
        bg, fg = JAM_COLORS[level]
        since = time.monotonic() - r["since"]
        self.banner.configure(text=f"{level.upper()}   {r['summary']}"
                              + (f"   (since {since:.0f} s)"
                                 if level not in ("ok", "learning") else ""),
                              bg=bg, fg=fg)
        order = ("learning", "ok", "watch", "warning", "alarm")
        if order.index(level) > order.index(self.jam_level) and \
                level in ("warning", "alarm"):
            self.root.bell()
            print(time.strftime("%H:%M:%S"), "JAMMING", level, r["summary"],
                  file=sys.stderr, flush=True)
        self.jam_level = level
        self.root.title(("⚠ " + level.upper() + " · " if level in ("warning", "alarm")
                         else "") + "NMEA 0183 monitor")

        t = self.jam_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", f"{'Band':<5} {'Sats':>4} {'Lost':>4} {'Baseline':>9} "
                        f"{'Now':>8} {'Median drop':>12} {'Dropped ≥6 dB':>14}  Level\n", "dim")
        for band in sorted(r["bands"]):
            b = r["bands"][band]
            now_snr = "--" if b["current_snr"] is None else f"{b['current_snr']:.0f} dB"
            t.insert("end", f"{band:<5} {b['sats']:>4} {b['lost']:>4} "
                            f"{b['baseline_snr']:>6.0f} dB {now_snr:>8} "
                            f"{b['median_drop']:>9.1f} dB {b['fraction_dropped']:>13.0%}  ")
            t.insert("end", b["level"] + "\n", b["level"])
        rx = r["receiver"]
        t.insert("end", "Receiver detector ($PAIRSPF): ", "dim")
        if rx:
            for band in sorted(rx):
                lvl = {0: "learning", 1: "ok", 2: "warning", 3: "alarm"}[rx[band]]
                t.insert("end", f"{band} {PAIRSPF_STATUS[rx[band]]}   ", lvl)
            t.insert("end", "\n")
        else:
            t.insert("end", "not reported (enable with: python3 lc29h_jamming.py "
                            "--enable)\n", "dim")
        fix = {True: "valid", False: "LOST", None: "unknown"}[r["fix_ok"]]
        t.insert("end", f"Position fix: {fix}\n", "dim" if r["fix_ok"] else "alarm")
        for when, lvl, text in self.jam.events[-4:][::-1]:
            t.insert("end", f"{when}  ", "dim")
            t.insert("end", f"{lvl:<8}", lvl)
            t.insert("end", f" {text}\n")
        t.delete("end-2c", "end-1c")
        lines = int(t.index("end-1c").split(".")[0])
        t.configure(height=lines, state="disabled")

    def close(self):
        self.stop.set()
        self.root.destroy()

    def field_for(self, msg):
        f = self.fields.get(msg["key"])
        if f is None:
            if msg["kind"] in ("VDM", "VDO"):
                f = Field(self, self.ais_area, msg["key"], msg["kind"], msg["talker"])
                f.frame.pack(side="top", fill="x", padx=4, pady=4)
            else:
                f = Field(self, self.grid_area, msg["key"], msg["kind"], msg["talker"])
                n = self.n_grid
                self.n_grid += 1
                f.frame.grid(row=n // self.columns, column=n % self.columns,
                             sticky="nsew", padx=4, pady=4)
            self.fields[msg["key"]] = f
        return f

    def poll(self):
        for _ in range(2000):
            try:
                what, value = self.queue.get_nowait()
            except queue.Empty:
                break
            if what == "status":
                self.conn = value
            elif what == "reset":
                self.decoder.clear()
            elif value:
                self.lines += 1
                msg = self.decoder.decode(value)
                if msg is None:
                    self.bad += 1
                    continue
                if msg["kind"] == "GSA" and msg["data"]:
                    d = msg["data"]
                    sysid = d["system_id"] or TALKER_SYSTEM_ID.get(d["talker"])
                    self.used[sysid] = {p.lstrip("0") for p in d["used"]}
                if msg["data"] and not msg["error"]:
                    if msg["kind"] == "GSV":
                        self.jam.update_gsv(msg["data"])
                    elif msg["kind"] in ("RMC", "GGA", "GSA"):
                        self.jam.update_fix(msg["data"])
                    elif msg["kind"] in ("PAIRSPF", "PAIRSPF5"):
                        self.jam.update_receiver(msg["data"])
                self.field_for(msg).update(msg)
        for f in self.fields.values():
            if f.dirty:
                f.render()
        self.root.after(200, self.poll)

    def tick(self):
        now = time.monotonic()
        for f in self.fields.values():
            f.tick(now)
        self.show_jamming(self.jam.assess(now))
        self.status.configure(text=f"{self.conn}   ·   {self.lines} lines, "
                                   f"{self.bad} invalid, {len(self.fields)} sentence types")
        self.root.after(1000, self.tick)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default=settings.HOST)
    p.add_argument("--port", type=int, default=settings.PORT)
    p.add_argument("--columns", type=int, default=2)
    a = p.parse_args()
    root = tk.Tk()
    w = int(root.winfo_screenwidth() * 0.9)
    h = int(root.winfo_screenheight() * 0.85)
    root.geometry(f"{w}x{h}")
    App(root, a.host, a.port, max(1, a.columns))
    root.mainloop()


if __name__ == "__main__":
    main()
