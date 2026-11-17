"""Tests for the ais_stream runtime: ticks on a quiet stream, Ctrl-C and restarts."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import ais_stream


class OwnShip:
    def get_mmsi(self): return 244000001
    def get_latitude(self): return 52.037
    def get_longitude(self): return 4.032
    def get_cog(self): return 0.0
    def get_sog(self): return 6.0


def position_report(mmsi=211000001):
    return SimpleNamespace(msg_type=1, mmsi=mmsi, lat=52.047, lon=4.032,
                           course=180.0, speed=6.0, status=0)


class WithTicksTests(unittest.TestCase):
    def test_yields_items_then_ends(self):
        self.assertEqual(list(ais_stream.with_ticks(iter([1, 2, 3]))), [1, 2, 3])

    def test_yields_none_while_source_is_quiet(self):
        release = threading.Event()

        def slow():
            release.wait(5)
            yield "msg"

        ticks = ais_stream.with_ticks(slow(), interval=0.05)
        self.assertIsNone(next(ticks))
        release.set()
        self.assertEqual([item for item in ticks if item is not None], ["msg"])

    def test_reraises_reader_error(self):
        def broken():
            yield 1
            raise TimeoutError("stream silent")

        ticks = ais_stream.with_ticks(broken())
        self.assertEqual(next(ticks), 1)
        with self.assertRaises(TimeoutError):
            next(ticks)


class QuietStreamRefreshTests(unittest.TestCase):
    def test_tick_refreshes_cpa_without_new_message(self):
        clock = iter([0.0, 0.0, 1.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        with patch.object(ais_stream, "now", side_effect=lambda: next(clock, 10.0)), \
                patch.object(ais_stream, "refresh_cpa", return_value=[]) as refresh:
            # one report, then only ticks: the refresh must still run
            ais_stream.track_messages([position_report(), None, None], None, OwnShip())
        refresh.assert_called()

    def test_stop_event_ends_tracking_on_tick(self):
        stop = threading.Event()
        stop.set()

        def endless_ticks():
            while True:
                yield None

        done = threading.Thread(target=ais_stream.track_messages,
                                args=(endless_ticks(), None, OwnShip(), stop))
        done.start()
        done.join(2)
        self.assertFalse(done.is_alive())


class ShutdownAndRestartTests(unittest.TestCase):
    def test_keyboard_interrupt_propagates_from_do_track(self):
        stream = MagicMock()
        stream.__enter__.return_value = stream
        with patch.object(ais_stream, "open_stream", return_value=stream), \
                patch.object(ais_stream, "track_messages", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ais_stream.do_track(None, OwnShip())
        stream._fobj.shutdown.assert_called_once()

    def test_keyboard_interrupt_ends_track_forever(self):
        stop = threading.Event()
        with patch.object(ais_stream, "do_track", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ais_stream.track_forever(None, OwnShip(), stop)

    def test_unexpected_error_restarts_tracking(self):
        stop = threading.Event()
        calls = []

        def fail_then_stop(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("bug")
            stop.set()

        with patch.object(ais_stream, "do_track", side_effect=fail_then_stop), \
                patch.object(ais_stream, "RECONNECT_SECONDS", 0), \
                self.assertLogs(ais_stream.log, "ERROR") as logs:
            ais_stream.track_forever(None, OwnShip(), stop)
        self.assertEqual(len(calls), 2)
        self.assertIn("AIS tracking failed", logs.output[0])

    def test_sigterm_handler_raises_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            ais_stream.interrupt_on_sigterm(15, None)


if __name__ == "__main__":
    unittest.main()
