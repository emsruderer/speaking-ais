import asyncio
import can
from nmea2000.device import N2KDevice
from nmea2000.message import NMEA2000Message

async def handle_received(message: NMEA2000Message) -> None:
    print(f"Received PGN {message.PGN} from source {message.source}")

async def main() -> None:
    device = N2KDevice.for_python_can(interface="socketcan", channel="can0")
    # Additional setup and message handling logic here


from nmea2000.decoder import NMEA2000Decoder

# Initialize decoder
decoder = NMEA2000Decoder()

# Connect to CAN bus (e.g. slcan device on /dev/ttyUSB0)
bus = can.interface.Bus(interface='actisense', channel="/dev/ttyUSB2", bitrate=125000)

# Decode frames
for msg in bus:
    decoded_frame = decoder.decode(msg)

    # Print decoded frame when ready (fast data intermediate frames return None)
    if decoded_frame is not None:
        print(decoded_frame)

"""
import can


bus = can.interface.Bus("can0", interface="socketcan")
for msg in bus:
    pgn = (msg.arbitration_id >> 8) & 0x3FFFF
    if pgn == 65309:
        d = msg.data
        mfr = (d[0] | d[1] << 8) & 0x7FF
        print(f"src={msg.arbitration_id & 0xFF} mfr={mfr} "
              f"status={d[2]} batt={d[3]}% charge={d[4]}%")
"""
