"""Tests for multi-process queue monitor status formatting."""

import unittest
from contextlib import nullcontext
import multiprocessing
from unittest.mock import Mock

from monitor_window import queue_status_text
from queue_metrics import increment, read


class QueueStatusTests(unittest.TestCase):
    def test_formats_queue_occupancy_as_approximate(self):
        que = Mock()
        que.qsize.return_value = 3
        enqueued = Mock(value=11)
        dequeued = Mock(value=8)
        enqueued.get_lock.return_value = nullcontext()
        dequeued.get_lock.return_value = nullcontext()

        self.assertEqual(
            queue_status_text(que, 100, enqueued, dequeued),
            "3 / 100 queued (approximate) | added: 11 | removed: 8",
        )

    def test_reports_unsupported_queue_size(self):
        que = Mock()
        que.qsize.side_effect = NotImplementedError

        self.assertEqual(
            queue_status_text(que, 100),
            "Queue size unavailable | added: 0 | removed: 0",
        )

    def test_shared_counters_track_activity(self):
        counter = multiprocessing.Value("L", 0)

        increment(counter)

        self.assertEqual(read(counter), 1)


if __name__ == "__main__":
    unittest.main()
