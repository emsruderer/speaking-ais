"""Tests for the multi-process AIS pipeline."""

import multiprocessing
import time
import queue
import socket
import threading
import unittest
from unittest.mock import patch

from pyais.encode import encode_dict
from pyais.stream import IterMessages

import ais_stream
import multi_process_ais as mpa
import settings
from cpa_tracker import CPATrack


def nmea(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}"


def raw_messages(*messages):
    lines = [line.encode() for msg in messages for line in encode_dict(msg)]
    return list(IterMessages(lines))


class FakeShip:
    def get_position(self):
        return 53.0, 7.0


class DecoderTests(unittest.TestCase):
    def decode(self, raws):
        raw_que = queue.Queue()
        for raw in raws:
            raw_que.put(raw)


        class StopWhenEmpty:
            def is_set(self):
                return raw_que.empty()

        decoder = mpa.Decoder(raw_que, queue.Queue(), FakeShip(), StopWhenEmpty())
        with patch.object(settings, "VERBOSE", False):
            return list(decoder.messages())

    def test_keeps_static_data_and_vessels_in_range_only(self):
        decoded = self.decode(raw_messages(
            {"type": 1, "mmsi": 1, "lat": 53.05, "lon": 7.0},   # 3 NM
            {"type": 1, "mmsi": 2, "lat": 53.25, "lon": 7.0},   # 15 NM
            {"type": 1, "mmsi": 3, "lat": 91, "lon": 181},      # no position
            {"type": 5, "mmsi": 4, "shipname": "FAR AWAY"},
            {"type": 4, "mmsi": 5},                             # base station
        ))
        self.assertEqual([msg.mmsi for msg in decoded], [1, 4])

    def test_in_range_uses_nautical_miles(self):
        radius = settings.AIS_RANGE_NM
        self.assertTrue(mpa.position_in_range(
            53.0 + (radius - 0.1) / 60, 7.0, 53.0, 7.0))
        self.assertFalse(mpa.position_in_range(
            53.0 + (radius + 0.1) / 60, 7.0, 53.0, 7.0))

    def test_range_can_be_changed_or_disabled_in_settings(self):
        raws = raw_messages(
            {"type": 1, "mmsi": 1, "lat": 53.02, "lon": 7.0},  # 1.2 NM
            {"type": 1, "mmsi": 2, "lat": 53.05, "lon": 7.0},  # 3 NM
        )
        with patch.object(settings, "AIS_RANGE_NM", 2.0):
            self.assertEqual([msg.mmsi for msg in self.decode(raws)], [1])
        with patch.object(settings, "AIS_RANGE_NM", None):
            self.assertEqual([msg.mmsi for msg in self.decode(raws)], [1, 2])


class PipelineTest(unittest.TestCase):
    """Open water thresholds: the target is 2 NM away."""

    def setUp(self):
        patcher = patch.object(settings, "NAVIGATION_MODE", "open")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reader_and_decoder_processes_produce_a_warning(self):
        # target 2 NM north of the own ship (Jemgum), heading south at 6 kn
        target = encode_dict({"type": 1, "mmsi": 211000001,
                              "lat": mpa.JEMGUM_LAT + 2 / 60, "lon": mpa.JEMGUM_LON,
                              "speed": 6.0, "course": 180.0})[0]
        static = encode_dict({"type": 5, "mmsi": 211000001, "shipname": "TESTER",
                              "ship_type": 52})
        gps = nmea("GPRMC,120000,A,5315.8274,N,00723.8428,E,6.0,000.0,051026")
        payload = ("\r\n".join([gps, *static, target]) + "\r\n").encode()

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def serve():
            connections = []
            listener.settimeout(10)
            try:
                for _ in range(2):  # the AIS reader and the own-ship GPS reader
                    conn, _ = listener.accept()
                    conn.sendall(payload)
                    connections.append(conn)
                threading.Event().wait(5)
            except OSError:
                pass
            finally:
                for conn in connections:
                    conn.close()

        threading.Thread(target=serve, daemon=True).start()
        raw_que = multiprocessing.Queue(10)
        warn_que = multiprocessing.Queue(10)
        stop = multiprocessing.Event()
        reader = multiprocessing.Process(target=mpa.do_read,
                                         args=(raw_que, "127.0.0.1", port), daemon=True)
        decoder = multiprocessing.Process(target=mpa.run_decoder,
                                          args=(raw_que, warn_que, stop, "127.0.0.1", port))
        reader.start()
        decoder.start()
        try:
            track = warn_que.get(timeout=10)
        finally:
            stop.set()
            decoder.join(timeout=10)
            reader.terminate()
            reader.join()
            listener.close()

        self.assertIsInstance(track, CPATrack)
        self.assertEqual(track.mmsi, 211000001)
        self.assertEqual(track.shipname, "TESTER")
        self.assertLess(track.cpa, 0.1)
        self.assertEqual(decoder.exitcode, 0)


def echo_first(que, _counter=None):
    """Stand-in warning process: hand back the first queued item."""
    ECHO.put(que.get())


ECHO = multiprocessing.Queue()


class SupervisorTests(unittest.TestCase):
    def test_restart_after_kill_inside_get(self):
        ctx = multiprocessing.get_context("fork")
        que = ctx.Queue(10)
        with patch.object(ais_stream, "run_warning_process", echo_first):
            first = ais_stream.start_warning_process(que)
            time.sleep(0.5)  # now blocked in que.get(), holding the reader lock
            first.kill()
            first.join()
            second = ais_stream.supervise_warning_process(first, que)
        try:
            self.assertIsNot(second, first)
            que.put("warning")
            self.assertEqual(ECHO.get(timeout=5), "warning")
        finally:
            second.kill()
            second.join()


if __name__ == "__main__":
    unittest.main()
