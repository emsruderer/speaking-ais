"""Tests for the AIS window lifecycle."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from extra.ais_message_window import AISMessageWindow


class AISMessageWindowTests(unittest.TestCase):
    def test_close_stops_streams_before_destroying_window(self):
        window = AISMessageWindow.__new__(AISMessageWindow)
        window.stop_event = threading.Event()
        window.own_ship = SimpleNamespace(stop=Mock())
        window.root = SimpleNamespace(destroy=Mock())

        window.close()

        self.assertTrue(window.stop_event.is_set())
        window.own_ship.stop.assert_called_once_with()
        window.root.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
