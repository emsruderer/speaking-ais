"""
Script to read CAN messages and extract specific PGN data.

Hardware: Waveshare USB-CAN-A (CH340, 1a86:7523). This adapter speaks the
same serial protocol as the Seeed USB-CAN Analyzer, so python-can's
"seeedstudio" interface is used.
"""
import can

PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"


def parse_id(arb_id):
    prio = (arb_id >> 26) & 0x7
    dp = (arb_id >> 24) & 0x3
    pf = (arb_id >> 16) & 0xFF
    ps = (arb_id >> 8) & 0xFF
    sa = arb_id & 0xFF
    if pf < 240:
        pgn = (dp << 16) | (pf << 8)
    else:
        pgn = (dp << 16) | (pf << 8) | ps
    return prio, pgn, sa


def decode(pgn, d):
    "Decode N2K message based on PGN."
    # Returns a tuple of decoded values if PGN matches, otherwise None.
    if pgn == 65309:
        if len(d) < 5:
            return None
        mfr = (d[0] | d[1] << 8) & 0x7FF
        status = d[2]
        batt = d[3]
        charge = d[4]
        return mfr, status, batt, charge
    elif pgn == 65312:                       # Navico wireless signal status
            if len(d) < 5:
                return None
            header = int.from_bytes(d[0:2], "little")
            manufacturer = header & 0x7FF
            industry = (header >> 13) & 0x07
            field_a = d[4] if d[4] < 0x80 else d[4] - 0x100
            return manufacturer, industry, d[2], d[3], field_a
    return None


bus = can.Bus(
    interface="seeedstudio",
    channel=PORT,
    baudrate=2000000,         # USB-serial speed of the adapter (fixed)
    bitrate=250000,           # NMEA 2000 bus speed
    frame_type="EXT",         # N2K uses 29-bit IDs; default "STD" would filter them out
    operation_mode="normal",  # "silent" = listen-only (no ACKs on the bus)
)
try:
    for msg in bus:
        if not msg.is_extended_id:
            continue
        prio, pgn, sa = parse_id(msg.arbitration_id)
        result = decode(pgn, msg.data)
        if result:
            if pgn == 65309:
                mfr, status, batt, charge = result
                print(f"src={sa} mfr={mfr} status={status} batt={batt}% charge={charge}%")
            elif pgn == 65312:
                manufacturer, industry, unknown, signal_strength, field_a = result
                print(f"src={sa} manufacturer={manufacturer} industry={industry} "
                      f"unknown={unknown} signal={signal_strength}% field_a={field_a}")
            print(f"prio={prio} pgn={pgn}")
            print(f"sa={sa}")
finally:
    bus.shutdown()
