"""Threaded AIS receiver that reports nearby vessels' CPA and TCPA."""

import logging
import socket
import threading
import time

from cpa_tcpa import cpa_tcpa
from ais_stream import own_velocity, target_velocity
from cpa_tracker import available
from pyais.filter import haversine
from pyais.stream import TCPConnection
from ships import Ship

HOST = "localhost"
PORT = 10110
RANGE_KM = 15.0
SHIP_TTL_SECONDS = 600  # forget vessels not heard from for this long
OWN_MMSI = 244030153
OWN_LAT = 53.26417
OWN_LON = 7.396137

log = logging.getLogger(__name__)


class Receiver(threading.Thread):
    """Collect current CPA data for AIS position reports within a radius."""

    def __init__(self, my_ship, host=HOST, port=PORT, range_km=RANGE_KM):
        super().__init__(name="AISReceiver", daemon=True)
        self.my_ship = my_ship
        self.host = host
        self.port = port
        self.range_km = range_km
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._connection_lock = threading.Lock()
        self._connection = None
        self._ships = {}
        self._last_seen = {}

    def in_range(self, lat, lon, minimum=None):
        """Return whether a valid position is within the configured radius."""
        if lat is None or lon is None:
            return False
        radius = self.range_km if minimum is None else minimum
        own_lat, own_lon = self.my_ship.get_position()
        distance_km = haversine((own_lat, own_lon), (lat, lon))
        return distance_km <= radius

    def get_ships(self):
        """Return a thread-safe snapshot of current vessel CPA results."""
        with self._lock:
            now = time.monotonic()
            for mmsi, seen in list(self._last_seen.items()):
                if now - seen > SHIP_TTL_SECONDS:
                    del self._last_seen[mmsi]
                    self._ships.pop(mmsi, None)
            return {mmsi: data.copy() for mmsi, data in self._ships.items()}

    def process_message(self, message):
        """Update nearby-vessel CPA data from one decoded AIS message."""
        if message.msg_type not in (1, 2, 3, 18):
            return
        lat = available("lat", message.lat)
        lon = available("lon", message.lon)
        if lat is None or lon is None:
            return
        if not self.in_range(lat, lon):
            return

        cpa_data = cpa_tcpa(
            self.my_ship.get_mmsi(),
            self.my_ship.get_latitude(),
            self.my_ship.get_longitude(),
            *own_velocity(self.my_ship),
            message.mmsi,
            lat,
            lon,
            *target_velocity(available("course", message.course),
                             available("speed", message.speed)),
        )
        with self._lock:
            self._ships[message.mmsi] = cpa_data
            self._last_seen[message.mmsi] = time.monotonic()

    def run(self):
        """Read AIS messages until stopped or the TCP stream closes."""
        try:
            with TCPConnection(self.host, self.port) as connection:
                with self._connection_lock:
                    self._connection = connection
                    stopped = self.stop_event.is_set()
                if stopped:
                    self._interrupt_connection(connection)
                    return
                try:
                    for sentence in connection:
                        if self.stop_event.is_set():
                            return
                        try:
                            self.process_message(sentence.decode())
                        except Exception as error:  # pyais raises its own types
                            log.warning("Skipping malformed AIS message: %s", error)
                finally:
                    with self._connection_lock:
                        if self._connection is connection:
                            self._connection = None
        except OSError as error:
            if not self.stop_event.is_set():
                log.error("AIS connection to %s:%s failed: %s",
                          self.host, self.port, error)

    @staticmethod
    def _interrupt_connection(connection):
        """Interrupt a blocking socket read so shutdown can complete promptly."""
        try:
            connection._fobj.shutdown(socket.SHUT_RDWR)
        except OSError as error:
            log.debug("AIS connection was already closed during shutdown: %s", error)

    def stop(self):
        """Stop the receiver and wait briefly for its worker thread to finish."""
        self.stop_event.set()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            self._interrupt_connection(connection)
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5.0)
            if self.is_alive():
                log.warning("AIS receiver thread did not stop before timeout")


def print_ships(ships):
    """Print a snapshot of vessel CPA data."""
    for mmsi, data in ships.items():
        tcpa = data["tcpa"]
        if tcpa is None:
            print(f"{mmsi}: CPA unavailable")
        elif tcpa < 0:
            print(
                f"{mmsi}: closest point passed, range {data['distance']:.2f} NM, "
                f"bearing {data['bearing']:.1f}°"
            )
        elif tcpa == float("inf"):
            print(
                f"{mmsi}: CPA {data['cpa']:.2f} NM, no relative motion, "
                f"range {data['distance']:.2f} NM"
            )
        else:
            print(
                f"{mmsi}: CPA {data['cpa']:.2f} NM in {tcpa:.1f} min, "
                f"range {data['distance']:.2f} NM, bearing {data['bearing']:.1f}°"
            )


def main():
    logging.basicConfig(level=logging.INFO)
    own_ship = Ship(OWN_MMSI, OWN_LAT, OWN_LON, 287, 5.0, HOST, PORT)
    receiver = Receiver(own_ship)
    own_ship.start()
    receiver.start()
    try:
        while True:
            time.sleep(10)
            print_ships(receiver.get_ships())
    except KeyboardInterrupt:
        log.info("Shutdown requested")
    finally:
        receiver.stop()
        own_ship.stop()


if __name__ == "__main__":
    main()
