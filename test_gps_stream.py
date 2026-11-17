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


    def test_pypilot_heading_and_attitude_sentences(self):
        hdm = gps_stream.decode_line("$APHDM,0.248,M*3D")
        self.assertEqual(hdm, {"sentence": "HDM", "magnetic_heading": 0.248})
        self.assertNotIn("heading", hdm)     # Ship treats "heading" as true

        hdg = gps_stream.decode_line(nmea("HCHDG,358.0,2.0,W,3.0,E"))
        self.assertEqual(hdg["deviation"], -2.0)
        self.assertEqual(hdg["magnetic_heading"], 356.0)
        self.assertEqual(hdg["true_heading"], 359.0)
        empty = gps_stream.decode_line("$IIHDG,0.25,,,0.00,E*25")
        self.assertIsNone(empty["deviation"])
        self.assertEqual(empty["true_heading"], 0.25)
        self.assertIsNone(gps_stream.decode_line(nmea("IIHDG,10,,,2,X")))

        pitch = gps_stream.decode_line("$APXDR,A,-0.595,D,PTCH*5F")
        m, = pitch["measurements"]
        self.assertEqual((m["label"], m["value"], m["unit_text"]), ("pitch", -0.595, "°"))
        text = gps_stream.decode_text_line(nmea("YXXDR,C,19.5,C,AIRT,P,1.013,B,BARO"))
        self.assertEqual(text, "XDR  AIRT  19.5 °C  temperature; BARO  1.013 bar  pressure")
        self.assertIsNone(gps_stream.decode_line(nmea("APXDR,A,1.0,D")))




def _cycle(talker, sig, snrs):
    """Analysed GSV cycle with satellites spread evenly around the sky."""
    sats = [{"prn": str(10 + i), "el": 40, "az": i * 360 // len(snrs), "snr": snr}
            for i, snr in enumerate(snrs)]
    return gps_stream.analyze_gsv(talker, sig, len(sats), sats)


class JammingMonitorTests(unittest.TestCase):
    def feed(self, mon, t0, seconds, l1, l5, fix="A"):
        r = None
        for k in range(seconds):
            now = t0 + k
            mon.update_gsv(_cycle("GP", "1", l1), now)
            mon.update_gsv(_cycle("GP", "8", l5), now)
            mon.update_fix({"sentence": "RMC", "status": fix}, now)
            r = mon.assess(now)
        return r

    def learned(self):
        mon = gps_stream.JammingMonitor()
        r = self.feed(mon, 0, 40, [40] * 8, [38] * 8)
        self.assertEqual(r["level"], "ok")
        return mon

    def test_learning_before_baseline(self):
        mon = gps_stream.JammingMonitor()
        r = self.feed(mon, 0, 5, [40] * 8, [38] * 8)
        self.assertEqual(r["level"], "learning")

    def test_l1_drop_with_normal_l5_is_jamming(self):
        mon = self.learned()
        r = self.feed(mon, 40, 5, [26] * 8, [38] * 8)
        self.assertEqual(r["level"], "alarm")
        self.assertIn("L1-band interference", r["summary"])
        self.assertEqual(r["bands"]["L5"]["level"], "ok")

    def test_onset_is_debounced(self):
        mon = self.learned()
        r = self.feed(mon, 40, 2, [26] * 8, [38] * 8)
        self.assertEqual(r["level"], "ok")

    def test_directional_obstruction_is_not_jamming(self):
        mon = self.learned()
        r = self.feed(mon, 40, 10, [40] * 5 + [20] * 3, [38] * 5 + [18] * 3)
        self.assertIn(r["level"], ("ok", "watch"))

    def test_lost_satellites_and_fix_raise_alarm_then_recover(self):
        mon = self.learned()
        r = self.feed(mon, 40, 6, [None] * 8, [None] * 8, fix="V")
        self.assertEqual(r["level"], "alarm")
        self.assertIn("all bands affected", r["summary"])
        self.assertIn("fix lost", r["summary"])
        r = self.feed(mon, 46, 5, [40] * 8, [38] * 8)
        self.assertEqual(r["level"], "alarm")          # still clearing
        self.assertIn("recovering", r["summary"])
        r = self.feed(mon, 51, 12, [40] * 8, [38] * 8)
        self.assertEqual(r["level"], "ok")

    def test_baseline_frozen_during_jamming(self):
        mon = self.learned()
        self.feed(mon, 40, 300, [26] * 8, [38] * 8)
        r = self.feed(mon, 340, 1, [26] * 8, [38] * 8)
        self.assertEqual(r["level"], "alarm")


class ReceiverJammingTests(unittest.TestCase):
    def test_pairspf_and_ack_are_decoded(self):
        dec = gps_stream.NmeaFieldDecoder()
        self.assertEqual(nmea("PAIRSPF,1"), "$PAIRSPF,1*52")      # spec example
        m = dec.decode("$PAIRSPF5,3*65")
        self.assertEqual(m["key"], "$PAIRSPF5")
        self.assertEqual(m["data"]["band"], "L5")
        self.assertEqual(m["data"]["status_text"], "critical")
        ack = dec.decode("$PAIR001,391,0*30")
        self.assertEqual(ack["data"], {"sentence": "PAIR001", "command": "391",
                                       "result": "OK"})
        self.assertIsNotNone(dec.decode("$PAIRSPF,7*" + nmea("PAIRSPF,7")[-2:])["error"])

    def test_receiver_status_raises_level(self):
        mon = gps_stream.JammingMonitor()
        feed = JammingMonitorTests().feed
        feed(mon, 0, 40, [40] * 8, [38] * 8)
        r = None
        for k in range(40, 45):
            mon.update_gsv(_cycle("GP", "1", [40] * 8), k)
            mon.update_gsv(_cycle("GP", "8", [38] * 8), k)
            mon.update_receiver({"band": "L1", "status": 2}, k)
            r = mon.assess(k)
        self.assertEqual(r["level"], "warning")
        self.assertIn("Receiver reports L1 warning", r["summary"])
        self.assertIn("signal levels look normal", r["summary"])
        # stale receiver status is ignored
        r = mon.assess(60)
        self.assertEqual(r["receiver"], {})

    def test_proprietary_sentences_do_not_break_decode_line(self):
        self.assertIsNone(gps_stream.decode_line("$PAIRSPF,1*52"))


if __name__ == "__main__":
    unittest.main()
