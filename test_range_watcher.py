import unittest

from ais_stream import METERS_PER_NM, RangeWatcher


class RangeWatcherTest(unittest.TestCase):
    def feed(self, watcher, distances_m, step=5):
        for i, metres in enumerate(distances_m):
            watcher.update(1, i * step, metres / METERS_PER_NM)
        return watcher.stalled(1, (len(distances_m) - 1) * step)

    def test_constant_range_is_stalled(self):
        jitter = [400, 403, 398, 401, 399, 402, 400, 397, 401, 400]
        self.assertTrue(self.feed(RangeWatcher(), jitter))

    def test_closing_range_is_not_stalled(self):
        closing = [400 - 10 * i for i in range(10)]
        self.assertFalse(self.feed(RangeWatcher(), closing))

    def test_short_history_is_not_stalled(self):
        self.assertFalse(self.feed(RangeWatcher(), [400, 400, 400]))

    def test_growing_range_is_stalled(self):
        self.assertTrue(self.feed(RangeWatcher(), [400 + 10 * i for i in range(10)]))

    def test_forget_clears_history(self):
        watcher = RangeWatcher()
        self.feed(watcher, [400] * 10)
        watcher.forget(1)
        self.assertFalse(watcher.stalled(1, 100))


if __name__ == "__main__":
    unittest.main()
