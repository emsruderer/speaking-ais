"""Helpers for process-shared queue activity counters."""


def increment(counter):
    """Increment a multiprocessing.Value counter when one is configured."""
    if counter is None:
        return
    with counter.get_lock():
        counter.value += 1


def read(counter):
    """Read a multiprocessing.Value counter consistently."""
    if counter is None:
        return 0
    with counter.get_lock():
        return counter.value
