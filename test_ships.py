"""Tests for Ship navigation decoding and dead reckoning."""

import unittest
import socket
import threading
from unittest.mock import patch

import ships
from ships import Ship, gps_utc


def nmea(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}\r\n".encode()


class ShipTests(unittest.TestCase):
    def setUp(self):
        self.ship = Ship(123456789, 53.0, 7.0, 0.0, 0.0)

    def test_vtg_course_is_degrees_and_empty_fields_keep_previous_values(self):
        self.ship.decode_vtg("GPVTG,084.4,T,083.0,M,022.4,N,041.5,K")
        self.assertAlmostEqual(self.ship.get_cog(), 84.4)
        self.assertAlmostEqual(self.ship.get_sog(), 22.4)

        self.ship.decode_vtg("GPVTG,,T,,M,,N,,K")
        self.assertAlmostEqual(self.ship.get_cog(), 84.4)
        self.assertAlmostEqual(self.ship.get_sog(), 22.4)

    def test_rmc_handles_empty_speed_and_invalid_fix(self):
        result = self.ship.decode_rmc(
            "GPRMC,123519,A,4807.038,N,01131.000,E,,084.4,230394,,,A"
        )
        self.assertAlmostEqual(self.ship.get_latitude(), 48.1173)
        self.assertAlmostEqual(self.ship.get_longitude(), 11.5166666667)
        self.assertIsNone(result["sog"])

        self.ship.decode_rmc("GPRMC,123520,V,,,,,,,230394,,,N")
        self.assertTrue(self.ship.deadreckoning)

    def test_dead_reckoning_uses_degrees_and_elapsed_time(self):
        self.ship.set_cog(90)
        self.ship.sog = 6
        self.ship.deadreckoning = True
        self.ship._last_deadreckoning_update = 100.0

        with patch("ships.time.monotonic", return_value=3700.0):
            self.ship._advance_dead_reckoning()

        self.assertAlmostEqual(self.ship.get_latitude(), 53.0)
        self.assertAlmostEqual(
            self.ship.get_longitude(),
            7.0 + 6.0 / (60.0 * 0.6018150232),
            places=6,
        )

    def test_stale_gps_position_switches_to_dead_reckoning(self):
        self.ship.sog = 6
        self.ship.deadreckoning = False
        self.ship._last_position_update = 100.0
        self.ship._last_deadreckoning_update = 100.0

        with patch("ships.time.monotonic", return_value=3700.0):
            self.ship._advance_dead_reckoning()

        self.assertTrue(self.ship.deadreckoning)
        self.assertAlmostEqual(self.ship.get_latitude(), 53.1)

    def test_stream_uses_configured_host_and_decodes_complete_sentence(self):
        line = nmea("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394")

        class FakeConnection:
            def __init__(self):
                self.chunks = [line, b""]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def settimeout(self, _):
                pass

            def recv(self, _):
                return self.chunks.pop(0)

        connection = FakeConnection()
        with patch("ships.socket.create_connection", return_value=connection) as connect:
            stream = self.ship.gps_from_signalk(host="gps.example", port=1234)
            decoded = next(stream)
            self.assertEqual(decoded["sentence"], "RMC")
            self.assertAlmostEqual(decoded["latitude"], 48.1173)
            with self.assertRaises(ConnectionError):
                next(stream)

        connect.assert_called_once_with(("gps.example", 1234), timeout=3.0)

    def test_invalid_rmc_does_not_change_speed_or_course(self):
        self.ship.decode_rmc("GPRMC,123519,A,4807.038,N,01131.000,E,5.0,090.0,230394")
        self.ship.decode_rmc("GPRMC,123520,V,,,,,22.0,270.0,230394")
        self.assertAlmostEqual(self.ship.get_sog(), 5.0)
        self.assertAlmostEqual(self.ship.get_cog(), 90.0)

    def test_dead_reckoning_continues_while_ais_keeps_stream_busy(self):
        ais = b"!AIVDM,1,1,,A,15M67FC000G?ufbE`FepT@3n00Sa,0*5C\r\n"
        clock = [0.0]
        ship = self.ship

        class BusyConnection:
            """Delivers an AIS line every 5 s, so recv() never times out."""
            chunks = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def settimeout(self, _):
                pass

            def recv(self, _):
                self.chunks += 1
                if self.chunks > 12:  # 60 s of AIS-only traffic
                    ship._stop_event.set()
                clock[0] += 5.0
                return ais

        ship.sog = 6.0
        ship.set_cog(0.0)
        with patch("ships.time.monotonic", side_effect=lambda: clock[0]), \
                patch("ships.socket.create_connection", return_value=BusyConnection()), \
                patch("ships.decode_line") as decode:
            ship._apply_navigation({"latitude": 53.0, "longitude": 7.0})
            list(ship.gps_from_signalk())

        decode.assert_not_called()  # AIS lines are skipped, not decoded
        self.assertTrue(ship.deadreckoning)
        # 65 s at 6 kn due north from the last fix
        self.assertAlmostEqual(ship.get_latitude(), 53.0 + 6.0 * 65 / 3600 / 60, places=7)

    def test_stop_closes_a_quiet_stream_thread(self):
        connected = threading.Event()

        class QuietConnection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def settimeout(self, _):
                connected.set()

            def recv(self, _):
                raise socket.timeout

        with patch("ships.socket.create_connection", return_value=QuietConnection()):
            self.ship.start()
            self.assertTrue(connected.wait(timeout=1.0))
            self.ship.stop()

        self.assertFalse(self.ship.is_alive())


class FixModeTests(unittest.TestCase):
    def test_rmc_mode_is_decoded_and_logged_on_change(self):
        ship = Ship(123456789, 53.0, 7.0, 0.0, 0.0)
        decoded = ship.decode_rmc("GPRMC,123519,A,4807.038,N,01131.000,E,,084.4,230394,,,A")
        self.assertEqual(decoded.get("mode") or ships.decode_rmc(
            "GPRMC,123519,A,4807.038,N,01131.000,E,,084.4,230394,,,A".split(","))["mode"], "A")
        with self.assertLogs("ships", "WARNING") as logs:
            ship.note_fix_mode({"mode": "A"})
            ship.note_fix_mode({"mode": "A"})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("without SBAS", logs.output[0])




class SystemClockTests(unittest.TestCase):
    RMC = {"sentence": "RMC", "status": "A", "date": "061026", "time_utc": "120000.50"}

    def setUp(self):
        self.ship = Ship(123456789, 53.0, 7.0, 0.0, 0.0)
        self.ship._clock_sync = True

    def test_gps_utc_parses_valid_rmc_only(self):
        self.assertEqual(gps_utc(self.RMC), 1791288000.5)
        self.assertIsNone(gps_utc({**self.RMC, "status": "V"}))
        self.assertIsNone(gps_utc({**self.RMC, "date": None}))
        self.assertIsNone(gps_utc({**self.RMC, "date": "061006"}))  # week rollover

    def test_sets_clock_only_when_far_off(self):
        with patch("ships.time.clock_settime") as settime:
            with patch("ships.time.time", return_value=1791288002.0):
                self.ship.sync_system_clock(self.RMC)
            settime.assert_not_called()
            with patch("ships.time.time", return_value=1700000000.0):
                self.ship.sync_system_clock(self.RMC)
            settime.assert_called_once_with(ships.time.CLOCK_REALTIME, 1791288000.5)

    def test_missing_permission_disables_clock_sync(self):
        with patch("ships.time.time", return_value=0.0), \
                patch("ships.time.clock_settime", side_effect=PermissionError) as settime:
            self.ship.sync_system_clock(self.RMC)
            self.ship.sync_system_clock(self.RMC)
        settime.assert_called_once()

if __name__ == "__main__":
    unittest.main()
