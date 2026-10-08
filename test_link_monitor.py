"""Tests for spoken GPS/AIS loss alerts."""

import queue
import unittest
from unittest.mock import patch

import link_monitor as lm


class FakeShip:
    def __init__(self):
        self.age = 0.0

    def position_age(self):
        return self.age


class LinkMonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        patcher = patch("link_monitor.time.monotonic", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ship = FakeShip()
        self.que = queue.Queue()
        self.monitor = lm.LinkMonitor(self.ship, self.que)

    def texts(self):
        return [alert.text for alert in self.monitor.check()]

    def test_no_alert_while_data_flows(self):
        self.clock[0] = 100
        self.monitor.ais_seen()
        self.assertEqual(self.texts(), [])

    def test_gps_lost_repeated_then_restored(self):
        self.ship.age = 31
        self.assertEqual(self.texts(), [lm.GPS_LOST])
        self.clock[0] += 60
        self.assertEqual(self.texts(), [])            # not repeated yet
        self.clock[0] += lm.ALERT_REPEAT_SECONDS
        self.monitor.ais_seen()
        self.assertEqual(self.texts(), [lm.GPS_LOST])  # repeated while lost
        self.ship.age = 1
        self.assertEqual(self.texts(), [lm.GPS_RESTORED])
        self.assertEqual(self.que.qsize(), 3)

    def test_ais_lost_and_restored(self):
        self.clock[0] = lm.AIS_LOST_SECONDS + 1
        self.assertEqual(self.texts(), [lm.AIS_LOST])
        self.monitor.ais_seen()
        self.assertEqual(self.texts(), [lm.AIS_RESTORED])

    def test_both_lost_is_one_alert(self):
        self.ship.age = 1000
        self.clock[0] = lm.AIS_LOST_SECONDS + 1
        self.assertEqual(self.texts(), [lm.BOTH_LOST])

    def test_full_queue_does_not_raise(self):
        self.monitor.que = queue.Queue(1)
        self.monitor.que.put("busy")
        self.ship.age = 31
        with self.assertLogs("link_monitor", "ERROR"):
            self.monitor.check()


if __name__ == "__main__":
    unittest.main()
