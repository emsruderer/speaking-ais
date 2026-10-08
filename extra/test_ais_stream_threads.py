"""Tests for the threaded AIS receiver."""

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from extra.ais_stream_threads import Receiver


class FakeShip:
    def get_position(self):
        return 53.0, 7.0

    def get_mmsi(self):
        return "244030153"

    def get_latitude(self):
        return 53.0

    def get_longitude(self):
        return 7.0

    def get_cog(self):
        return 90.0

    def get_sog(self):
        return 5.0


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.receiver = Receiver(FakeShip())

    def test_position_message_calculates_cpa_with_own_ship_first(self):
        message = SimpleNamespace(
            msg_type=1,
            mmsi=123456789,
            lat=53.1,
            lon=7.1,
            course=180.0,
            speed=4.0,
        )
        result = {
            "mmsi": 123456789,
            "cpa": 0.5,
            "tcpa": 8.0,
            "distance": 8.4,
            "bearing": 30.0,
            "approaching_speed": 3.0,
        }
        with patch(
                "extra.ais_stream_threads.cpa_tcpa",
                return_value=result) as calculate:
            self.receiver.process_message(message)

        self.assertEqual(self.receiver.get_ships(), {123456789: result})
        self.assertEqual(calculate.call_args.args[:5],
                         ("244030153", 53.0, 7.0, 90.0, 5.0))
        self.assertEqual(calculate.call_args.args[5:],
                         (123456789, 53.1, 7.1, 180.0, 4.0))

    def test_ignores_unsupported_or_invalid_position_messages(self):
        unsupported = SimpleNamespace(msg_type=5, lat=53.1, lon=7.1)
        invalid_position = SimpleNamespace(
            msg_type=1, lat=None, lon=None, mmsi=123456789,
        )
        with patch(
                "extra.ais_stream_threads.cpa_tcpa") as calculate:
            self.receiver.process_message(unsupported)
            self.receiver.process_message(invalid_position)

        calculate.assert_not_called()
        self.assertEqual(self.receiver.get_ships(), {})

    def test_not_available_values_are_not_used(self):
        no_position = SimpleNamespace(msg_type=1, mmsi=1, lat=91.0, lon=181.0,
                                      course=360.0, speed=102.3)
        no_motion = SimpleNamespace(msg_type=1, mmsi=2, lat=53.1, lon=7.1,
                                    course=360.0, speed=102.3)
        with patch("extra.ais_stream_threads.cpa_tcpa", return_value={}) as calculate:
            self.receiver.process_message(no_position)
            self.receiver.process_message(no_motion)

        calculate.assert_called_once()
        self.assertEqual(calculate.call_args.args[5:], (2, 53.1, 7.1, 0.0, 0.0))

    def test_vessels_not_heard_from_are_forgotten(self):
        message = SimpleNamespace(msg_type=1, mmsi=1, lat=53.1, lon=7.1,
                                  course=0.0, speed=0.0)
        with patch("extra.ais_stream_threads.cpa_tcpa", return_value={}), \
                patch("extra.ais_stream_threads.time.monotonic", return_value=0.0):
            self.receiver.process_message(message)
        with patch("extra.ais_stream_threads.time.monotonic", return_value=500.0):
            self.assertIn(1, self.receiver.get_ships())
        with patch("extra.ais_stream_threads.time.monotonic", return_value=700.0):
            self.assertEqual(self.receiver.get_ships(), {})

    def test_get_ships_returns_a_snapshot(self):
        self.receiver._ships[1] = {"cpa": 0.5}

        snapshot = self.receiver.get_ships()
        snapshot[1]["cpa"] = 9.0
        snapshot[2] = {"cpa": 2.0}

        self.assertEqual(self.receiver.get_ships(), {1: {"cpa": 0.5}})

    def test_stop_interrupts_an_idle_tcp_read(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        receiver = Receiver(FakeShip(), host="127.0.0.1",
                            port=listener.getsockname()[1])
        peer = None
        try:
            receiver.start()
            peer, _ = listener.accept()
            receiver.stop()
            receiver.join(timeout=2)
            self.assertFalse(receiver.is_alive())
        finally:
            if peer is not None:
                peer.close()
            listener.close()
            receiver.stop()
            receiver.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
