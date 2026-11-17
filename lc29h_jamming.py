#!/home/nanno/.pyenv/shims/python3
"""Switch the Quectel LC29H receiver's own jamming detection on or off.

    python3 lc29h_jamming.py --enable [--save]
    python3 lc29h_jamming.py --disable [--save]
    python3 lc29h_jamming.py --status

--enable sends $PAIR391,1 so the receiver outputs $PAIRSPF (L1) and
$PAIRSPF5 (L5) jamming status at 1 Hz: 0 unknown, 1 no jamming, 2 warning,
3 critical. --save also sends $PAIR513 to keep the setting after power-off.
--status only watches the stream for $PAIRSPF sentences.

The command is written to the GPS serial port. While SignalK has that port
open, the replies are read from SignalK's NMEA 0183 TCP stream; otherwise
directly from the serial port. If the port is locked, stop SignalK first:
    sudo systemctl stop signalk; python3 lc29h_jamming.py --enable --save; \
    sudo systemctl start signalk
"""
import argparse
import socket
import sys
import time

import serial

import settings
from gps_stream import NmeaFieldDecoder

GPS_PORT = ("/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_"
            "Controller_8856c7539834f111a8fbd94dbe0872e1-if00-port0")
GPS_BAUD = 115200


def sentence(body):
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n"


class Lines:
    """Read NMEA lines from SignalK TCP if available, else from the serial port."""

    def __init__(self, ser, host, port):
        self.ser, self.sock, self.buf = ser, None, b""
        try:
            self.sock = socket.create_connection((host, port), timeout=3)
            self.sock.settimeout(0.5)
            self.source = f"SignalK {host}:{port}"
        except OSError:
            self.source = "serial port"

    def __iter__(self):
        while True:
            if self.sock:
                try:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        return                        # SignalK closed the stream
                except socket.timeout:
                    chunk = b""
            else:
                chunk = self.ser.read(4096)
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            for raw in lines:
                yield raw.decode("ascii", "replace").strip()
            if not chunk:
                yield None                            # lets the caller time out


def wait_for(lines, decoder, want, timeout):
    """Return the first decoded message for which want(msg) is true, or None."""
    end = time.monotonic() + timeout
    for line in lines:
        if time.monotonic() > end:
            return None
        if not line:
            continue
        msg = decoder.decode(line)
        if msg and msg["data"] and want(msg):
            return msg
    return None


def send(ser, lines, decoder, body):
    command = body[4:7]
    print(f"-> {sentence(body).strip()}")
    ser.write(sentence(body).encode("ascii"))
    ser.flush()
    ack = wait_for(lines, decoder, lambda m: m["kind"] == "PAIR001"
                   and m["data"]["command"].lstrip("0") == command.lstrip("0"), 5)
    if ack is None:
        print(f"   no $PAIR001 acknowledgement for {command} within 5 s")
        return False
    print(f"<- {ack['raw']}   ({ack['data']['result']})")
    return ack["data"]["result"] == "OK"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    g.add_argument("--status", action="store_true")
    p.add_argument("--save", action="store_true",
                   help="save the setting to flash ($PAIR513)")
    p.add_argument("--device", default=GPS_PORT)
    p.add_argument("--baud", type=int, default=GPS_BAUD)
    p.add_argument("--host", default=settings.HOST)
    p.add_argument("--port", type=int, default=settings.PORT)
    a = p.parse_args()

    ser = None
    if not a.status:
        try:
            ser = serial.Serial(a.device, a.baud, timeout=0.5)
        except serial.SerialException as e:
            print(f"Cannot open {a.device}: {e}\nIf SignalK locks the port, run:\n"
                  "  sudo systemctl stop signalk\n"
                  f"  python3 {sys.argv[0]} {' '.join(sys.argv[1:])}\n"
                  "  sudo systemctl start signalk", file=sys.stderr)
            return 1
    lines = Lines(ser, a.host, a.port)
    if lines.sock is None and ser is None:
        print("SignalK stream not reachable", file=sys.stderr)
        return 1
    print(f"Reading replies from {lines.source}")
    decoder = NmeaFieldDecoder()
    ok = True

    if a.enable or a.disable:
        ok = send(ser, lines, decoder, "PAIR391," + ("1" if a.enable else "0"))
        if ok and a.save:
            ok = send(ser, lines, decoder, "PAIR513")
    if a.disable:
        return 0 if ok else 1

    seen = {}
    end = time.monotonic() + 5
    while time.monotonic() < end and len(seen) < 2:
        msg = wait_for(lines, decoder,
                       lambda m: m["kind"] in ("PAIRSPF", "PAIRSPF5"),
                       end - time.monotonic())
        if msg is None:
            break
        seen[msg["data"]["band"]] = msg
    if not seen:
        print("No $PAIRSPF jamming status received within 5 s."
              + ("" if a.enable else " Enable it with --enable."))
        return 1
    for band in sorted(seen):
        d = seen[band]
        print(f"<- {d['raw']}   {band}: {d['data']['status_text']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
