#!/home/nanno/.pyenv/shims/python3

import os
import time
import serial
import matplotlib
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    matplotlib.use("Agg")                  # headless: alleen PNG opslaan
import matplotlib.pyplot as plt

DLE, STX, ETX = 0x10, 0x02, 0x03
PLOT_FILE = "ws320_battery.png"
PLOT_INTERVAL = 10                         # s tussen plot-updates

def decode(pgn, d):
    "Decode N2K message based on PGN."
    # Returns a tuple of decoded values if PGN matches, otherwise None. 
    if pgn == 65309:
        mfr = (d[0] | d[1] << 8) & 0x7FF
        status = d[2]
        batt = d[3]
        charge = d[4]
        return mfr, status, batt, charge
    elif pgn == 65312:  # Navico Wireless Signal Status
        if len(d) < 8:
            return None

        header = int.from_bytes(d[0:2], "little")
        manufacturer = header & 0x7FF
        industry = (header >> 13) & 0x07
        unknown = d[2]
        signal_strength = d[3]
        field_a = d[4] if d[4] < 0x80 else d[4] - 0x100
        return manufacturer, industry, unknown, signal_strength, field_a
    return None

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
                if in_frame: buf.append(DLE)
            elif b == STX:
                buf.clear(); in_frame = True
            elif b == ETX and in_frame:
                in_frame = False
                if sum(buf) % 256 == 0:
                    yield bytes(buf[:-1])      # zonder checksum
            continue
        if b == DLE:
            esc = True
        elif in_frame:
            buf.append(b)

NGT_MSG_SEND = 0xA1                        # BST: commando naar NGT-1
NGT_STARTUP_MSG = bytes([0x11, 0x02, 0x00])  # operating mode: receive all PGNs

def bst_write(ser, cmd, data):
    "Send a BST frame (DLE STX cmd len data crc DLE ETX) with DLE escaping."
    body = bytes([cmd, len(data)]) + bytes(data)
    body += bytes([(-sum(body)) & 0xFF])     # checksum: som van alles == 0 mod 256
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

class BatteryPlot:
    "Collects batt%/charge% over time and draws them against elapsed hours."

    def __init__(self):
        self.t0 = time.monotonic()
        self.hours, self.batt, self.charge = [], [], []
        self.last_draw = 0.0
        self.live = matplotlib.get_backend().lower() != "agg"
        self.fig, self.ax = plt.subplots(figsize=(10, 5))
        (self.l_batt,) = self.ax.plot([], [], label="Batt status %")
        (self.l_charge,) = self.ax.plot([], [], label="Charge %")
        self.ax.set_xlabel("Time [hr]")
        self.ax.set_ylabel("%")
        self.ax.set_ylim(0, 105)
        self.ax.set_title("WS320 battery (PGN 65309)")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="best")
        if self.live:
            plt.ion()
            plt.show(block=False)

    def add(self, batt, charge):
        self.hours.append((time.monotonic() - self.t0) / 3600)
        self.batt.append(batt)
        self.charge.append(charge)
        if time.monotonic() - self.last_draw >= PLOT_INTERVAL:
            self.draw()

    def draw(self):
        self.last_draw = time.monotonic()
        self.l_batt.set_data(self.hours, self.batt)
        self.l_charge.set_data(self.hours, self.charge)
        self.ax.set_xlim(0, max(self.hours[-1] if self.hours else 0, 0.01))
        if self.live:
            self.fig.canvas.draw_idle()
            plt.pause(0.001)

    def save(self):
        self.draw()
        self.fig.savefig(PLOT_FILE, dpi=120, bbox_inches="tight")
        print(f"Plot opgeslagen: {PLOT_FILE} ({len(self.hours)} punten)")


ser = serial.Serial("/dev/serial/by-id/usb-Actisense_NGT-1_35600-if00-port0",
                    115200, timeout=1)
init_ngt1(ser)
plot = BatteryPlot()
try:
    for f in bst_frames(ser):
        cmd, length = f[0], f[1]
        data = f[2:2 + length]
        if cmd == 0x93:                            # N2K-bericht ontvangen
            prio = data[0]
            pgn  = int.from_bytes(data[1:4], "little")
            dst, src = data[4], data[5]
            n    = data[10]
            payload = data[11:11 + n]
            #print(f"SA {src} PGN {pgn}: {payload.hex()}")
            result = decode(pgn, payload)        # decode() van eerder
            if result:
                if pgn == 65309:
                    mfr, status, batt, charge = result
                    print(f"src={src} mfr={mfr} status={status} batt={batt}% charge={charge}%")
                    plot.add(batt, charge)
                elif pgn == 65312:
                    manufacturer, industry, unknown, signal_strength, field_a = result
                    print(
                        f"src={src} manufacturer={manufacturer} industry={industry} "
                        f"unknown={unknown} signal={signal_strength}% field_a={field_a}"
                    )
except KeyboardInterrupt:
    pass
finally:
    plot.save()
    if plot.live:
        plt.ioff()
        plt.show()
