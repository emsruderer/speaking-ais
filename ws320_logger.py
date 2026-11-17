#!/home/nanno/.pyenv/shims/python3
"""
WS320 batterij-logger via Actisense NGT-1.

- PGN 65309 (Navico): batterijstatus % en laadstroom % (van max)
- PGN 65312 (Navico): wireless signaalsterkte %
- Schrijft naar CSV met wandkloktijd (hervat bij herstart)
- Plot batt / charge / signal tegen de klok; headless wordt de PNG periodiek ververst
"""

import os
import csv
import time
from datetime import datetime

import serial
import matplotlib
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    matplotlib.use("Agg")                  # headless: alleen PNG opslaan
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

PORT = "/dev/serial/by-id/usb-Actisense_NGT-1_35600-if00-port0"
CSV_FILE = "ws320_log.csv"
PLOT_FILE = "ws320_battery.png"
PLOT_INTERVAL = 10                         # s tussen scherm-updates
SAVE_INTERVAL = 60                         # s tussen PNG-saves
LOG_INTERVAL = 60                          # s: max. tijd tussen CSV-regels als niets verandert
SIGNAL_TIMEOUT = 120                       # s: oudere signaalwaarde niet meer loggen

DLE, STX, ETX = 0x10, 0x02, 0x03
NGT_MSG_SEND = 0xA1                        # BST: commando naar NGT-1
NGT_STARTUP_MSG = bytes([0x11, 0x02, 0x00])  # operating mode: receive all PGNs


# ---------------------------------------------------------------- decoding

def decode(pgn, d):
    "Decode N2K message based on PGN. Returns tuple or None."
    if pgn == 65309:                       # Navico battery status
        if len(d) < 5:
            return None
        mfr = (d[0] | d[1] << 8) & 0x7FF
        return mfr, d[2], d[3], d[4]       # mfr, status, batt, charge
    if pgn == 65312:                       # Navico wireless signal status
        if len(d) < 5:
            return None
        header = int.from_bytes(d[0:2], "little")
        manufacturer = header & 0x7FF
        industry = (header >> 13) & 0x07
        field_a = d[4] if d[4] < 0x80 else d[4] - 0x100
        return manufacturer, industry, d[2], d[3], field_a
    return None


# ---------------------------------------------------------------- BST / NGT-1

def bst_frames(ser):
    buf, in_frame, esc = bytearray(), False, False
    while True:
        b = ser.read(1)
        if not b:
            continue
        b = b[0]
        if esc:
            esc = False
            if b == DLE:
                if in_frame:
                    buf.append(DLE)
            elif b == STX:
                buf.clear()
                in_frame = True
            elif b == ETX and in_frame:
                in_frame = False
                if sum(buf) % 256 == 0:
                    yield bytes(buf[:-1])  # zonder checksum
            continue
        if b == DLE:
            esc = True
        elif in_frame:
            buf.append(b)


def bst_write(ser, cmd, data):
    "Send a BST frame (DLE STX cmd len data crc DLE ETX) with DLE escaping."
    body = bytes([cmd, len(data)]) + bytes(data)
    body += bytes([(-sum(body)) & 0xFF])
    out = bytearray([DLE, STX])
    for b in body:
        out.append(b)
        if b == DLE:
            out.append(DLE)
    out += bytes([DLE, ETX])
    ser.write(out)
    ser.flush()


def init_ngt1(ser):
    "Put the Actisense NGT-1 into 'receive all PGNs' mode."
    ser.reset_input_buffer()
    bst_write(ser, NGT_MSG_SEND, NGT_STARTUP_MSG)


# ---------------------------------------------------------------- CSV log

class CsvLog:
    "Append-only CSV; schrijft bij wijziging of minstens elke LOG_INTERVAL s."

    FIELDS = ["time", "batt", "charge", "signal"]

    def __init__(self, path):
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.FIELDS)
            self.f.flush()
        self.last_vals = None
        self.last_write = 0.0

    def maybe_write(self, ts, batt, charge, signal):
        vals = (batt, charge, signal)
        now = time.monotonic()
        if vals == self.last_vals and now - self.last_write < LOG_INTERVAL:
            return False
        self.w.writerow([ts.isoformat(timespec="seconds"), batt, charge,
                         "" if signal is None else signal])
        self.f.flush()
        self.last_vals, self.last_write = vals, now
        return True

    def close(self):
        self.f.close()


def load_history(path):
    "Lees bestaande CSV zodat de plot na een herstart doorloopt."
    t, b, c, s = [], [], [], []
    if not os.path.exists(path):
        return t, b, c, s
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                t.append(datetime.fromisoformat(row["time"]))
                b.append(int(row["batt"]))
                c.append(int(row["charge"]))
                s.append(int(row["signal"]) if row["signal"] else float("nan"))
            except (KeyError, ValueError):
                continue
    return t, b, c, s


# ---------------------------------------------------------------- plot

class BatteryPlot:
    "batt% / charge% / signal% tegen wandkloktijd."

    def __init__(self, history):
        self.t, self.batt, self.charge, self.signal = history
        self.last_draw = 0.0
        self.last_save = 0.0
        self.live = matplotlib.get_backend().lower() != "agg"
        self.fig, self.ax = plt.subplots(figsize=(11, 5))
        (self.l_batt,) = self.ax.plot([], [], lw=2, label="Batt status %")
        (self.l_charge,) = self.ax.plot([], [], lw=2, label="Charge % (van max)")
        (self.l_signal,) = self.ax.plot([], [], lw=1, ls="--", alpha=0.5,
                                        label="BT signal %")
        self.ax.set_ylabel("%")
        self.ax.set_ylim(0, 105)
        self.ax.set_title("WS320 battery (PGN 65309) / signal (PGN 65312)")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="upper left")
        loc = mdates.AutoDateLocator()
        self.ax.xaxis.set_major_locator(loc)
        self.ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
        if self.live:
            plt.ion()
            plt.show(block=False)
        if self.t:
            self.draw()

    def add(self, ts, batt, charge, signal):
        self.t.append(ts)
        self.batt.append(batt)
        self.charge.append(charge)
        self.signal.append(float("nan") if signal is None else signal)
        now = time.monotonic()
        if now - self.last_draw >= PLOT_INTERVAL:
            self.draw()
        if now - self.last_save >= SAVE_INTERVAL:
            self.save(quiet=True)

    def draw(self):
        self.last_draw = time.monotonic()
        self.l_batt.set_data(self.t, self.batt)
        self.l_charge.set_data(self.t, self.charge)
        self.l_signal.set_data(self.t, self.signal)
        if self.t:
            lo, hi = self.t[0], self.t[-1]
            if lo == hi:
                hi = datetime.fromtimestamp(hi.timestamp() + 60)
            self.ax.set_xlim(lo, hi)
        if self.live:
            self.fig.canvas.draw_idle()
            plt.pause(0.001)

    def save(self, quiet=False):
        self.draw()
        self.last_save = time.monotonic()
        tmp = PLOT_FILE + ".tmp.png"
        self.fig.savefig(tmp, dpi=120, bbox_inches="tight")
        os.replace(tmp, PLOT_FILE)         # atomair: nooit half geschreven PNG
        if not quiet:
            print(f"Plot opgeslagen: {PLOT_FILE} ({len(self.t)} punten)")


# ---------------------------------------------------------------- main

def main():
    ser = serial.Serial(PORT, 115200, timeout=1)
    init_ngt1(ser)
    log = CsvLog(CSV_FILE)
    plot = BatteryPlot(load_history(CSV_FILE))
    signal, signal_t = None, 0.0

    try:
        for f in bst_frames(ser):
            if len(f) < 2 or f[0] != 0x93:     # alleen ontvangen N2K-berichten
                continue
            data = f[2:2 + f[1]]
            if len(data) < 11:
                continue
            pgn = int.from_bytes(data[1:4], "little")
            src = data[5]
            payload = data[11:11 + data[10]]
            result = decode(pgn, payload)
            if not result:
                continue

            if pgn == 65312:
                manufacturer, industry, unknown, signal_strength, field_a = result
                signal, signal_t = signal_strength, time.monotonic()
                print(f"src={src} manufacturer={manufacturer} industry={industry} "
                      f"unknown={unknown} signal={signal_strength}% field_a={field_a}")

            elif pgn == 65309:
                mfr, status, batt, charge = result
                sig = signal if time.monotonic() - signal_t < SIGNAL_TIMEOUT else None
                ts = datetime.now().astimezone()
                print(f"{ts:%H:%M:%S} src={src} mfr={mfr} status={status} "
                      f"batt={batt}% charge={charge}%")
                if log.maybe_write(ts, batt, charge, sig):
                    plot.add(ts, batt, charge, sig)
    except KeyboardInterrupt:
        pass
    finally:
        log.close()
        plot.save()
        if plot.live:
            plt.ioff()
            plt.show()


if __name__ == "__main__":
    main()