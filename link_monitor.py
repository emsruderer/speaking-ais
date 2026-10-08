"""Spoken alerts when GPS or AIS data is lost, and when it returns."""

import dataclasses
import logging
import threading
import time
from queue import Full

from queue_metrics import increment

GPS_LOST_SECONDS = 30  # no valid fix for this long: dead reckoning is getting unreliable
AIS_LOST_SECONDS = 600  # quiet waters can be silent for minutes; base stations send every 10 s
ALERT_REPEAT_SECONDS = 600  # repeat a lost alert while the data stays lost
CHECK_SECONDS = 5

GPS_LOST = "Warning: GPS position lost. Collision warnings use dead reckoning."
GPS_RESTORED = "GPS position restored."
AIS_LOST = "Warning: AIS data lost. No vessel reports are received."
AIS_RESTORED = "AIS data restored."
BOTH_LOST = "Warning: GPS and AIS data lost. Check the SignalK connection."

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Alert:
    """A system alert for the warning process; spoken before any vessel warning."""
    kind: str  # "gps", "ais" or "link"; a newer alert of a kind replaces a queued one
    text: str


class LinkMonitor(threading.Thread):
    """Watch the age of the own-ship fix and of the last AIS sentence.

    Call ais_seen() for every received AIS sentence. Alerts go on the warning queue."""

    def __init__(self, ship, que, activity_counter=None,
                 gps_lost_seconds=GPS_LOST_SECONDS, ais_lost_seconds=AIS_LOST_SECONDS,
                 repeat_seconds=ALERT_REPEAT_SECONDS, interval=CHECK_SECONDS):
        super().__init__(name="link-monitor", daemon=True)
        self.ship = ship
        self.que = que
        self.activity_counter = activity_counter
        self.gps_lost_seconds = gps_lost_seconds
        self.ais_lost_seconds = ais_lost_seconds
        self.repeat_seconds = repeat_seconds
        self.interval = interval
        self.stop_event = threading.Event()
        self._last_ais = time.monotonic()
        self._lost = {"gps": False, "ais": False}
        self._last_alert = {"gps": None, "ais": None}

    def ais_seen(self):
        """Record that an AIS sentence arrived."""
        self._last_ais = time.monotonic()

    def check(self):
        """Compare data ages with the limits; return and queue the resulting alerts."""
        now = time.monotonic()
        lost = {
            "gps": self.ship.position_age() > self.gps_lost_seconds,
            "ais": now - self._last_ais > self.ais_lost_seconds,
        }
        due = {
            kind: lost[kind] and (
                not self._lost[kind]
                or now - self._last_alert[kind] >= self.repeat_seconds)
            for kind in lost
        }
        alerts = []
        if due["gps"] and due["ais"]:
            alerts.append(Alert("link", BOTH_LOST))
        elif due["gps"]:
            alerts.append(Alert("gps", GPS_LOST))
        elif due["ais"]:
            alerts.append(Alert("ais", AIS_LOST))
        for kind in lost:
            if due[kind]:
                self._last_alert[kind] = now
            if self._lost[kind] and not lost[kind]:
                alerts.append(Alert(kind, GPS_RESTORED if kind == "gps" else AIS_RESTORED))
            self._lost[kind] = lost[kind]
        for alert in alerts:
            log.error("%s", alert.text)
            self._send(alert)
        return alerts

    def _send(self, alert):
        try:
            self.que.put(alert, timeout=1)
            increment(self.activity_counter)
        except Full:
            log.error("Warning queue full; could not queue alert: %s", alert.text)

    def run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.check()
            except Exception:  # monitoring must keep running
                log.exception("Link check failed")

    def stop(self):
        self.stop_event.set()
