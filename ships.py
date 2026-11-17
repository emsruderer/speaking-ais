"""Own-ship navigation state sourced from a SignalK NMEA 0183 stream."""

import logging
import math
import socket
import threading
import time
from datetime import datetime, timezone

import settings
from dead_reckoning import DeadReckoning, course_speed
from gps_stream import (
    decode_gll,
    decode_hdt,
    decode_line,
    decode_rmc,
    decode_vtg,
    decode_zda,
)

log = logging.getLogger(__name__)
GPS_STALE_SECONDS = 10.0
CLOCK_TOLERANCE_SECONDS = 5.0  # leave smaller offsets to NTP; covers stream latency
MIN_GPS_YEAR = 2025  # earlier dates are GPS week-number rollover errors
FIX_MODES = {
    "A": "autonomous, without SBAS (EGNOS) correction; expect about 9 m position error",
    "D": "differential (SBAS/EGNOS or DGPS); expect a few meters position error",
    "E": "estimated (dead reckoning by the GPS receiver)",
}
NAVIGATION_SENTENCES = {"GLL", "RMC", "VTG", "ZDA", "HDT", "HDG", "HDM", "VHW", "XDR"}
HEEL_NAMES = {"HEEL", "ROLL"}  # XDR angle transducer names for the heel angle
OUTAGE_REPORT_SECONDS = 30.0  # log the dead-reckoning error after longer GPS outages
METERS_PER_NM = 1852.0


def moved(position, course, speed, seconds):
    """Position (lat, lon) after sailing course (degrees) at speed (knots)."""
    lat, lon = position
    distance_nm = speed * seconds / 3600.0
    course = math.radians(course)
    lat += distance_nm * math.cos(course) / 60.0
    latitude_cosine = math.cos(math.radians(lat))
    if abs(latitude_cosine) > 1e-12:
        lon += distance_nm * math.sin(course) / (60.0 * latitude_cosine)
    return lat, lon


def distance_m(a, b):
    """Distance in meters between two nearby (lat, lon) positions."""
    north = (a[0] - b[0]) * 60.0
    east = (a[1] - b[1]) * 60.0 * math.cos(math.radians(a[0]))
    return math.hypot(north, east) * METERS_PER_NM


def gps_utc(decoded):
    """Return the UTC time (Unix seconds) of a valid RMC fix, or None."""
    if decoded.get("sentence") != "RMC" or decoded.get("status") != "A":
        return None
    date, utc = decoded.get("date"), decoded.get("time_utc")
    try:
        stamp = datetime.strptime(date + utc[:6], "%d%m%y%H%M%S").replace(
            tzinfo=timezone.utc)
        fraction = float("0" + utc[6:]) if len(utc) > 6 else 0.0
    except (TypeError, ValueError):
        return None
    if stamp.year < MIN_GPS_YEAR:
        return None
    return stamp.timestamp() + fraction


class Ship(threading.Thread):
    """Maintain own-ship navigation data, falling back to dead reckoning."""

    def __init__(self, mmsi, lat, lon, course, speed,
                 host=settings.HOST, port=settings.PORT):
        super().__init__(name=f"Ship-{mmsi}", daemon=True)
        self.mmsi = int(mmsi)
        self.host = host
        self.port = port
        self.lat = float(lat)
        self.lon = float(lon)
        self.cog = float(course) % 360.0
        self.heading = self.cog
        self.sog = float(speed)
        self.minute = 60.0
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self.deadreckoning = True
        self.navigation = {}
        self.utc_time = None
        self.date = None
        self.day = None
        self.month = None
        self.year = None
        self.status = None
        self._clock_sync = settings.SET_SYSTEM_CLOCK
        self.fix_mode = None
        self._last_deadreckoning_update = time.monotonic()
        self._last_position_update = self._last_deadreckoning_update
        # the lambda lets tests patch time.monotonic
        self.model = DeadReckoning(clock=lambda: time.monotonic())
        self._had_fix = False
        self._outage = None  # dead-reckoning state since the last fix
        self._update_projected_position()
        self.get_navigation()

    def _update_projected_position(self):
        self.y = self.lat * 60.0
        self.x = self.lon * 60.0 * math.cos(math.radians(self.lat))

    def _snapshot(self):
        return {
            "lat": self.lat,
            "lon": self.lon,
            "cog": self.cog,
            "sog": self.sog,
        }

    def get_mmsi(self):
        """Return this vessel's MMSI as a string."""
        return str(self.mmsi)

    def get_navigation(self):
        """Return a consistent snapshot of current navigation state."""
        with self._state_lock:
            self.navigation = self._snapshot()
            return self.navigation.copy()

    def _get_latitude(self):
        """Return latitude in nautical miles north of the equator."""
        with self._state_lock:
            return self.y

    def _get_longitude(self):
        """Return longitude projected to nautical miles at current latitude."""
        with self._state_lock:
            return self.x

    def get_cog(self):
        """Return course over ground in degrees."""
        with self._state_lock:
            return self.cog

    def set_cog(self, course):
        """Set course over ground in degrees."""
        with self._state_lock:
            self.cog = float(course) % 360.0

    def get_sog(self):
        """Return speed over ground in knots."""
        with self._state_lock:
            return self.sog

    def position_age(self):
        """Seconds since the last valid GPS fix (or since start when there was none)."""
        with self._state_lock:
            return time.monotonic() - self._last_position_update

    def get_position(self):
        """Return latitude and longitude in decimal degrees."""
        with self._state_lock:
            return self.lat, self.lon

    def get_latitude(self):
        """Return latitude in decimal degrees."""
        with self._state_lock:
            return self.lat

    def get_longitude(self):
        """Return longitude in decimal degrees."""
        with self._state_lock:
            return self.lon

    def _apply_navigation(self, decoded):
        """Apply fields from a decoded navigation sentence."""
        with self._state_lock:
            latitude = decoded.get("latitude")
            longitude = decoded.get("longitude")
            self._feed_model(decoded)
            if latitude is not None and longitude is not None:
                self._end_outage((latitude, longitude))
                self.model.update_fix(latitude, longitude)
                self._had_fix = True
                self.lat = latitude
                self.lon = longitude
                self.deadreckoning = False
                self._last_position_update = time.monotonic()
                self._last_deadreckoning_update = self._last_position_update
            elif decoded.get("status") == "V" and not self.deadreckoning:
                # Dead reckoning continues from the last valid fix.
                self.deadreckoning = True

            # Speed and course from a sentence flagged invalid (RMC status V) are not trusted.
            if decoded.get("status") in (None, "A"):
                speed = decoded.get("speed_over_ground", decoded.get("speed_knots"))
                if speed is not None:
                    self.sog = speed

                course = decoded.get("course_over_ground", decoded.get("true_track"))
                if course is not None:
                    self.cog = course % 360.0
                if speed is not None and course is not None:
                    self.model.update_ground(self.cog, self.sog)

            heading = decoded.get("heading")
            if heading is not None:
                self.heading = heading % 360.0

            self.utc_time = decoded.get("time_utc", self.utc_time)
            self.date = decoded.get("date", self.date)
            self.status = decoded.get("status", self.status)
            self.day = decoded.get("day", self.day)
            self.month = decoded.get("month", self.month)
            self.year = decoded.get("year", self.year)
            self._update_projected_position()
            self.navigation = self._snapshot()
            return self.navigation.copy()

    def _feed_model(self, decoded):
        """Pass heading, speed through water and heel to the dead-reckoning model."""
        sentence = decoded.get("sentence")
        if sentence == "HDT":
            self.model.update_heading(true_heading=decoded.get("heading"))
        elif sentence in ("HDG", "HDM"):
            self.model.update_heading(magnetic_heading=decoded.get("magnetic_heading"),
                                      sentence_variation=decoded.get("variation"))
        elif sentence == "VHW":
            self.model.update_stw(decoded.get("speed_through_water"))
        elif sentence == "XDR":
            for measurement in decoded.get("measurements", ()):
                if (measurement["type"] == "A"
                        and measurement["name"].upper() in HEEL_NAMES):
                    self.model.update_heel(measurement["value"])

    def _end_outage(self, fix):
        """On a fix after dead reckoning, log how far both methods were off."""
        outage, self._outage = self._outage, None
        if outage is None or not outage["had_fix"]:
            return
        seconds = time.monotonic() - outage["start"]
        if seconds < OUTAGE_REPORT_SECONDS:
            return
        log.warning(
            "GPS back after %.0f s; dead-reckoning error: %.0f m keeping course "
            "and speed, %.0f m with the sensor model (%s; %s)",
            seconds, distance_m(fix, outage["simple"]), distance_m(fix, outage["model"]),
            ", ".join(sorted(outage["sources"])), self.model.describe())

    def decode_gll(self, sentence: str) -> dict:
        """Decode a GLL sentence and update navigation state."""
        decoded = decode_gll(sentence.split("*", 1)[0].split(","))
        self._apply_navigation(decoded)
        return decoded

    def decode_vtg(self, sentence: str) -> dict:
        """Decode a VTG sentence and update course and speed."""
        decoded = decode_vtg(sentence.split("*", 1)[0].split(","))
        self._apply_navigation(decoded)
        return {
            "cog": self.get_cog(),
            "magnetic_track": decoded["magnetic_track"],
            "sog": self.get_sog(),
            "speed_kmh": decoded["speed_kmh"],
        }

    def decode_zda(self, sentence: str) -> dict:
        """Decode a ZDA sentence and update UTC date and time."""
        decoded = decode_zda(sentence.split("*", 1)[0].split(","))
        self._apply_navigation(decoded)
        return decoded

    def decode_hdt(self, sentence: str) -> dict:
        """Decode a true heading sentence and update heading."""
        decoded = decode_hdt(sentence.split("*", 1)[0].split(","))
        self._apply_navigation(decoded)
        return decoded

    def decode_rmc(self, sentence: str) -> dict:
        """Decode an RMC sentence and update position, course, and speed."""
        decoded = decode_rmc(sentence.split("*", 1)[0].split(","))
        self._apply_navigation(decoded)
        return {
            "time_utc": decoded["time_utc"],
            "status": decoded["status"],
            "latitude": self.get_latitude() if decoded["latitude"] is not None else None,
            "longitude": self.get_longitude() if decoded["longitude"] is not None else None,
            "sog": self.get_sog() if decoded["speed_over_ground"] is not None else None,
            "cog": self.get_cog() if decoded["course_over_ground"] is not None else None,
            "date": decoded["date"],
        }

    def gps_from_signalk(self, host=None, port=None, max_line_bytes=4096):
        """Yield decoded navigation sentences from one SignalK connection."""
        if max_line_bytes < 1:
            raise ValueError("max_line_bytes must be positive")
        host = self.host if host is None else host
        port = self.port if port is None else port

        with socket.create_connection((host, port), timeout=3.0) as connection:
            connection.settimeout(1.0)
            buffer = bytearray()
            while not self._stop_event.is_set():
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    self._advance_dead_reckoning()
                    continue
                if not chunk:
                    raise ConnectionError(f"SignalK connection to {host}:{port} closed")

                buffer.extend(chunk)
                *lines, remainder = buffer.split(b"\n")
                buffer = bytearray(remainder)
                for raw in lines:
                    if len(raw) > max_line_bytes:
                        log.warning("Dropping oversized NMEA line (%d bytes)", len(raw))
                        continue
                    if b"$" not in raw:
                        continue  # AIS (!AIVDM) and other non-navigation lines
                    decoded = decode_line(raw.decode("ascii", "replace").strip())
                    if (decoded is not None
                            and decoded.get("sentence") in NAVIGATION_SENTENCES):
                        yield decoded

                if len(buffer) > max_line_bytes:
                    buffer.clear()
                    raise ValueError(
                        f"NMEA line exceeds configured limit of {max_line_bytes} bytes"
                    )
                # The stream also carries AIS, so recv() may never time out while
                # GPS is lost; check for a stale fix after every chunk as well.
                self._advance_dead_reckoning()

    def _advance_dead_reckoning(self):
        """Advance the position by elapsed time while GPS is lost.

        Two tracks run from the last fix: the simple one keeps the last course
        and speed, the model one follows DeadReckoning.ground_velocity(). The
        published state follows the model when settings.DEAD_RECKONING_SENSORS
        is set, otherwise the simple track (the model runs in shadow)."""
        now = time.monotonic()
        with self._state_lock:
            if not self.deadreckoning:
                if now - self._last_position_update <= GPS_STALE_SECONDS:
                    return
                # Stale fix: extrapolate from the time of the last valid position.
                self.deadreckoning = True
            elapsed = now - self._last_deadreckoning_update
            self._last_deadreckoning_update = now
            if self._outage is None:
                self._outage = {
                    "start": self._last_position_update, "had_fix": self._had_fix,
                    "simple": (self.lat, self.lon), "model": (self.lat, self.lon),
                    "cog": self.cog, "sog": self.sog, "sources": set(), "source": "",
                }
            outage = self._outage
            if elapsed <= 0:
                return

            velocity, source = self.model.ground_velocity(now)
            if velocity is None:
                course, speed, source = outage["cog"], outage["sog"], "course and speed"
            else:
                course, speed = course_speed(velocity)
            if source != outage["source"]:
                log.info("Dead reckoning (%s): %s",
                         "sensor model" if settings.DEAD_RECKONING_SENSORS else "shadow",
                         source)
                outage["source"] = source
            outage["sources"].add(source)
            outage["simple"] = moved(outage["simple"], outage["cog"], outage["sog"], elapsed)
            outage["model"] = moved(outage["model"], course, speed, elapsed)
            if settings.DEAD_RECKONING_SENSORS:
                self.lat, self.lon = outage["model"]
                self.cog, self.sog = course, speed
            else:
                self.lat, self.lon = outage["simple"]
            self._update_projected_position()
            self.navigation = self._snapshot()

    def sync_system_clock(self, decoded):
        """Step the system clock to GPS time when it is more than a few seconds off.

        A Raspberry Pi has no real-time clock, so without internet (NTP) it boots
        with a wrong time. Needs CAP_SYS_TIME; without it this is disabled once."""
        if not self._clock_sync:
            return
        gps_time = gps_utc(decoded)
        if gps_time is None:
            return
        offset = gps_time - time.time()
        if abs(offset) <= CLOCK_TOLERANCE_SECONDS:
            return
        try:
            time.clock_settime(time.CLOCK_REALTIME, gps_time)
        except (PermissionError, OSError) as error:
            self._clock_sync = False
            log.warning("Cannot set system clock from GPS (%s); it is %.0f s off. "
                        "Grant CAP_SYS_TIME to enable this.", error, offset)
        else:
            log.warning("System clock set from GPS; it was %.1f s off", offset)

    def note_fix_mode(self, decoded):
        """Log when the GPS fix mode (RMC mode indicator) changes."""
        mode = decoded.get("mode")
        if mode is None or mode == self.fix_mode:
            return
        self.fix_mode = mode
        level = logging.INFO if mode == "D" else logging.WARNING
        log.log(level, "GPS fix mode %s: %s", mode, FIX_MODES.get(mode, "unknown"))

    def run(self):
        """Read own-ship navigation and reconnect when the stream disconnects."""
        while not self._stop_event.is_set():
            try:
                for decoded in self.gps_from_signalk():
                    if self._stop_event.is_set():
                        return
                    self._apply_navigation(decoded)
                    self.sync_system_clock(decoded)
                    self.note_fix_mode(decoded)
                    log.debug("Own-ship navigation: %s", self.get_navigation())
            except (OSError, ValueError) as error:
                if self._stop_event.is_set():
                    return
                log.warning("GPS stream error: %s; using dead reckoning", error)
                with self._state_lock:
                    self.deadreckoning = True

            if self._stop_event.wait(5.0):
                return
            self._advance_dead_reckoning()

    def stop(self):
        """Request shutdown and wait for the reader thread to finish."""
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5.0)
            if self.is_alive():
                log.warning("Ship thread did not stop before timeout")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ship = Ship(settings.OWN_MMSI, settings.OWN_START_LAT, settings.OWN_START_LON,
                0.0, 0.0)
    ship.start()
    try:
        while True:
            log.info("Own ship: %s", ship.get_navigation())
            time.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        ship.stop()
