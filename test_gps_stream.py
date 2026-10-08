import unittest
from unittest.mock import patch

import gps_stream


def nmea(body, marker="$"):
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"{marker}{body}*{checksum:02X}"


def armor(bits):
    fill = (-len(bits)) % 6
    bits += "0" * fill
    payload = ""
    for offset in range(0, len(bits), 6):
        value = int(bits[offset:offset + 6], 2)
        payload += chr(value + (48 if value < 40 else 56))
    return payload, fill


class GpsStreamTests(unittest.TestCase):
    def test_structured_and_text_decoders_are_both_available(self):
        line = nmea("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394")

        decoded = gps_stream.decode_line(line)
        text = gps_stream.decode_text_line(line)

        self.assertEqual(decoded["sentence"], "RMC")
        self.assertEqual(decoded["latitude"], 48.1173)
        self.assertIn("RMC", text)
        self.assertIn("latitude: 48.1173", text)

    def test_invalid_fix_and_coordinates_are_not_reported_as_positions(self):
        invalid_fix = nmea("GPGLL,4807.038,N,01131.000,E,123519,V")
        decoded = gps_stream.decode_line(invalid_fix)
        self.assertIsNone(decoded["latitude"])
        self.assertIsNone(decoded["longitude"])

        self.assertIsNone(gps_stream._latlon("9000.001", "N", 2))
        self.assertIsNone(gps_stream._latlon("1260.000", "N", 2))
        self.assertIsNone(gps_stream._latlon("4807.038", "X", 2))

    def test_bad_checksum_is_dropped(self):
        line = nmea("GPGLL,4807.038,N,01131.000,E,123519,A")
        self.assertIsNone(gps_stream.decode_line(line[:-2] + "00"))

    def test_ais_payload_requires_valid_armor_and_complete_message(self):
        with self.assertRaises(ValueError):
            gps_stream.parse_ais("x", 0)
        with self.assertRaises(ValueError):
            gps_stream.parse_ais("0", 0)
        with self.assertRaises(ValueError):
            gps_stream.unarmor("0", 6)

    def test_ais_fragments_are_assembled_and_decoded(self):
        bits = format(5, "06b") + "00" + format(123456789, "030b")
        bits += "0" * (424 - len(bits))
        payload, fill = armor(bits)
        first = nmea(f"AIVDM,2,1,7,A,{payload[:60]},0", "!")
        second = nmea(f"AIVDM,2,2,7,A,{payload[60:]},{fill}", "!")
        assembler = gps_stream.AisAssembler()

        self.assertIsNone(gps_stream.decode_line(first, assembler))
        decoded = gps_stream.decode_line(second, assembler)

        self.assertEqual(decoded["type"], 5)
        self.assertEqual(decoded["mmsi"], 123456789)
        self.assertFalse(assembler.pending)

    def test_gsv_text_decoder_keeps_cycle_state_per_assembler(self):
        decoder = gps_stream.NmeaTextDecoder()
        first = "GPGSV,2,1,1,1,30,100,40"
        second = "GPGSV,2,2,1"

        self.assertNotIn("Summary:", decoder.decode_line(nmea(first)))
        self.assertIn("Summary: 1 listed", decoder.decode_line(nmea(second)))

    def test_ais_incomplete_fragments_expire(self):
        bits = format(5, "06b") + "00" + format(123456789, "030b")
        bits += "0" * (424 - len(bits))
        payload, fill = armor(bits)
        assembler = gps_stream.AisAssembler(timeout=5)
        first = ["AIVDM", "2", "1", "7", "A", payload[:60], "0"]
        second = ["AIVDM", "2", "2", "7", "A", payload[60:], str(fill)]

        with patch("gps_stream.time.monotonic", return_value=0):
            self.assertIsNone(assembler.feed(first))
        with patch("gps_stream.time.monotonic", return_value=6):
            self.assertIsNone(assembler.feed(second))
        self.assertFalse(assembler.pending)

    def test_stream_drops_oversized_line_then_resynchronizes(self):
        valid = nmea("GPGLL,4807.038,N,01131.000,E,123519,A").encode()

        class FakeSocket:
            chunks = [b"x" * 120, b"x\n" + valid + b"\n", b""]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def recv(self, _):
                return self.chunks.pop(0)

        with patch("gps_stream.socket.create_connection", return_value=FakeSocket()):
            messages = list(gps_stream.gps_from_signalk(
                reconnect=False, max_line_bytes=100))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sentence"], "GLL")

    def test_invalid_gsv_counters_are_rejected(self):
        with self.assertRaises(ValueError):
            gps_stream.decode_gsv("GP", ["GPGSV", "2", "3", "4"])


if __name__ == "__main__":
    unittest.main()
