""" ais NMEA 0183 stream from SignalK port 10110 """
import collections
import dataclasses
import logging
import logging.handlers
import math
import queue as thread_queue
import signal
import socket
import sys
import threading
from collections.abc import Callable
from multiprocessing import Process, Queue
from pathlib import Path
from queue import Full

import settings
import warn
from cpa_tcpa import cpa_tcpa
from cpa_tracker import CPATrack, CPATracker, available, now
from link_monitor import LinkMonitor
from pyais.filter import FilterChain, MessageTypeFilter, NoneFilter
from pyais.stream import TCPConnection
from pyais.tracker import AISTrackEvent
from queue_metrics import increment
from ships import Ship

HOST = settings.HOST
PORT = settings.PORT
MAX_MESSAGES = 100
SPEECH_LANGUAGE = "en"
MINIMUM_CPA = 1 # nautical miles
MINIMUM_TCPA = 30  # minutes
MINIMUM_DISTANCE = 5 # nautical miles
# Always warn this long before CPA, whatever the distance: a 45 kn vessel covers
# MINIMUM_DISTANCE in under 6 minutes.
ALWAYS_WARN_TCPA = 10  # minutes
# Slow class B vessels (leisure craft) are agile; warn only for close encounters.
CLASS_B_TYPES = (18, 19, 24)
CLASS_B_SLOW_SPEED = 7  # knots
CLASS_B_SLOW_CPA = 0.3  # nautical miles
CLASS_B_SLOW_TCPA = 10  # minutes
METERS_PER_NM = 1852
# Below this speed over ground a vessel counts as stationary: at rest, GPS and AIS
# report 0.1-0.3 kn with a random course, which fakes relative motion.
STATIONARY_SPEED = 0.5  # knots
MOORED_STATUSES = (1, 5)  # AIS navigational status at anchor, moored
MOORED_MAX_SPEED = 1.0  # knots; above this a 'moored' status is assumed stale
OWN_SMOOTH_SECONDS = 30
OWN_STATIONARY_SPEED = 1.0  # knots; slower averaged own speed counts as at rest
# A vessel on a collision course must close the range. When the measured range
# changes less than RANGE_CLOSURE_MIN_M over RANGE_WINDOW_SECONDS, the computed
# TCPA comes from noisy speeds (GPS/AIS jitter) and no warning is given.
RANGE_WINDOW_SECONDS = 45
RANGE_CLOSURE_MIN_M = 20.0
UNKNOWN_HALF_BEAM_M = 3.0  # target half beam when AIS gives no dimensions
MANOEUVRE_SPEED = 3.0  # knots; own (and target) speed below this is manoeuvring
MANOEUVRE_LEAVE_SPEED = 3.5  # knots; own speed that ends manoeuvring (hysteresis)


@dataclasses.dataclass(frozen=True)
class DangerProfile:
    """Thresholds for the danger test (distances in NM, times in minutes)."""
    cpa: float
    tcpa: float
    distance: float
    always_warn_tcpa: float
    class_b_slow_cpa: float
    class_b_slow_tcpa: float
    smooth_reports: int = 1  # average a vessel's velocity over this many reports
    confirm_reports: int = 1  # consecutive dangerous reports before warning
    min_target_speed: float = 0.0  # knots; slower targets are ignored
    approximate: bool = False  # speak distances rounded, as 'about'
    name: str = "open"


PROFILES = {
    "open": DangerProfile(
        cpa=MINIMUM_CPA, tcpa=MINIMUM_TCPA, distance=MINIMUM_DISTANCE,
        always_warn_tcpa=ALWAYS_WARN_TCPA,
        class_b_slow_cpa=CLASS_B_SLOW_CPA, class_b_slow_tcpa=CLASS_B_SLOW_TCPA,
    ),
    # Rivers and channels: vessels normally pass at 30-100 m, so only a CPA well
    # inside that counts. AIS course noise (a 2 degree error is 65 m sideways at
    # 1 NM) makes a 50 m CPA reliable only within 0.5 NM, and only with the
    # velocity averaged over 5 reports and 2 consecutive dangerous reports;
    # simulated normal passings at 70 m then warn in 2-6 % instead of always.
    "river": DangerProfile(
        cpa=50 / METERS_PER_NM, tcpa=10, distance=0.5, always_warn_tcpa=0,
        class_b_slow_cpa=25 / METERS_PER_NM, class_b_slow_tcpa=5,
        smooth_reports=5, confirm_reports=2, approximate=True, name="river",
    ),
}

# Own vessel manoeuvring (mooring): for other slow vessels, only those moving
# (>= 1 kn) and passing within 15 m hull clearance inside 0.1 NM, announced once
# as a notice. With 2 x 9 m AIS position errors AIS cannot tell a routine
# harbour manoeuvre from a contact course.
HARBOUR_PROFILE = DangerProfile(
    cpa=15 / METERS_PER_NM, tcpa=5, distance=0.1, always_warn_tcpa=0,
    class_b_slow_cpa=15 / METERS_PER_NM, class_b_slow_tcpa=5,
    smooth_reports=5, confirm_reports=2, min_target_speed=1.0,
    approximate=True, name="harbour",
)


def danger_profile() -> DangerProfile:
    """The profile selected by settings.NAVIGATION_MODE."""
    try:
        return PROFILES[settings.NAVIGATION_MODE]
    except KeyError:
        raise ValueError(f"Unknown NAVIGATION_MODE {settings.NAVIGATION_MODE!r}; "
                         f"use one of {', '.join(PROFILES)}") from None


CPA_REFRESH_SECONDS = 5  # recompute CPA of all tracks between their reports
# A quiet stream still yields a tick this often, so CPA refreshes and stop
# requests do not wait for the next AIS message.
TICK_SECONDS = 1.0
CPA_KEYS = ("cpa", "tcpa", "distance", "bearing", "approaching_speed",
            "rel_east", "rel_north")
# SignalK also sends GPS sentences every second, so a stream silent this long is dead
STREAM_TIMEOUT_SECONDS = 60
RECONNECT_SECONDS = 5
LOG_FILE = Path(__file__).resolve().parent / "ais_stream.log"

log = logging.getLogger(__name__)

"""Define the filter chain with various criteria """
chain = FilterChain([
    MessageTypeFilter(1, 2, 3, 5, 18, 19, 24),
    NoneFilter('mmsi'),
    ])

def open_stream(host=HOST, port=PORT, timeout=STREAM_TIMEOUT_SECONDS):
    """Connect to the NMEA stream; a read blocking longer than timeout raises OSError,
    so a half-open connection is detected and reconnected."""
    stream = TCPConnection(host, port)
    stream._fobj.settimeout(timeout)
    return stream

def decoded_messages(stream, ais_seen=None):
    """Decode assembled AIS sentences, skipping any that pyais cannot decode.

    ais_seen is called for every sentence, including types that are filtered out."""
    for raw in stream:
        if ais_seen is not None:
            ais_seen()
        try:
            yield raw.decode()
        except Exception as error:  # a corrupt message must not stop tracking
            log.warning("Skipping undecodable AIS message: %s", error)

def pretty_print(tracks):
    """Pretty print a list of tracks to stdout."""
    headers = ['mmsi', 'callsign', 'shipname', 'lat', 'lon', 'cpa', 'tcpa', 'distance', 'bearing']
    rows = [[getattr(t, a) or 'N.A.' for a in headers] for t in tracks]
    row_format = "{:>20}" * (len(headers) + 1)
    sys.stdout.write(row_format.format(" ", *headers) + '\n')

    for i, row in enumerate(rows, start=1):
        sys.stdout.write(row_format.format(i, *row) +'\n')

def live_print(tracks):
    """Live print a list of tracks to stdout."""
    for _ in range(len(tracks) + 1):
        sys.stdout.write("\x1b[1A\x1b[2K")  # move up cursor and delete whole line
    pretty_print(tracks)


def danger(cpa, minimum_cpa=MINIMUM_CPA,minimum_tcpa=MINIMUM_TCPA, minimum_distance=MINIMUM_DISTANCE,
           always_warn_tcpa=ALWAYS_WARN_TCPA):
    """Determine if a CPA is dangerous.

    The CPA must lie ahead within minimum_tcpa and closer than minimum_cpa, and the
    vessel must be within minimum_distance or reach its CPA within always_warn_tcpa
    (so fast vessels are announced early enough). The last seconds before CPA count."""
    assert 'cpa' in cpa and 'tcpa' in cpa and 'distance' in cpa, "cpa dict must contain 'cpa', 'tcpa' and 'distance' keys"
    # nautical miles
    return (0 < cpa['tcpa'] < minimum_tcpa and -minimum_cpa < cpa['cpa'] < minimum_cpa
            and (cpa['distance'] < minimum_distance or cpa['tcpa'] < always_warn_tcpa))

def side_extent_m(track: CPATrack) -> float:
    """Largest distance from the target's AIS antenna to its side (meters)."""
    sides = [side for side in (track.to_port, track.to_starboard) if side]
    return max(sides) if sides else UNKNOWN_HALF_BEAM_M

def own_side_extent_m() -> float:
    """Largest distance from the own GPS antenna to the own vessel's side."""
    return settings.OWN_BEAM_M / 2 + abs(settings.OWN_ANTENNA_OFFSET_M)

def hull_clearance(track: CPATrack) -> float | None:
    """Hull-to-hull clearance at CPA in NM: the antenna CPA minus both side
    extents (conservatively the wider side of each vessel)."""
    if track.cpa is None:
        return None
    sides = (side_extent_m(track) + own_side_extent_m()) / METERS_PER_NM
    return max(0.0, track.cpa - sides)

def is_slow_class_b(track: CPATrack) -> bool:
    """Whether a track is a class B transponder with a known speed of at most 7 knots."""
    return (track.msg_type in CLASS_B_TYPES and track.speed is not None
            and track.speed <= CLASS_B_SLOW_SPEED)

def dangerous_track(track: CPATrack, profile: DangerProfile | None = None) -> bool:
    """Return whether a track has CPA data that meets the danger thresholds.

    The CPA limit applies to the hull-to-hull clearance."""
    if track.cpa is None or track.tcpa is None or track.distance is None:
        return False
    profile = profile or danger_profile()
    if profile.min_target_speed and (
            track.speed is None or track.speed < profile.min_target_speed):
        return False
    cpa = {"cpa": hull_clearance(track), "tcpa": track.tcpa, "distance": track.distance}
    if is_slow_class_b(track):
        return danger(cpa, minimum_cpa=profile.class_b_slow_cpa,
                      minimum_tcpa=profile.class_b_slow_tcpa,
                      minimum_distance=profile.distance,
                      always_warn_tcpa=profile.always_warn_tcpa)
    return danger(cpa, minimum_cpa=profile.cpa, minimum_tcpa=profile.tcpa,
                  minimum_distance=profile.distance,
                  always_warn_tcpa=profile.always_warn_tcpa)

def handle_create(track):
    """called every time an CPATrack is created"""
    log.debug('created: %s', track)

def handle_update(track):
    """called every time an CPATrack is updated"""
    log.debug('updated: %s', track)

def handle_delete(track):
    """called every time an CPATrack is deleted (pruned)"""
    log.debug   ('deleted: %s', track)

def queue_warning(que, track, activity_counter=None, profile=None):
    """Hand a copy of a dangerous track to the warning process without blocking.

    The warning process is slow (speech synthesis), so when it falls behind the
    track is dropped instead of stalling the AIS reader; the ship keeps
    reporting and will be queued again on its next position report."""
    profile = profile or danger_profile()
    try:
        que.put_nowait(dataclasses.replace(
            track, clearance=hull_clearance(track), mode=profile.name))
        increment(activity_counter)
    except Full:
        log.warning("Warning queue full; dropped warning for MMSI %s", track.mmsi)

def target_velocity(course, speed, status=None):
    """Course and speed for CPA. A vessel counts as stationary without a known
    course and speed (course 0 with a real speed would send it north), below
    STATIONARY_SPEED, or when at anchor or moored and slower than MOORED_MAX_SPEED."""
    if course is None or speed is None or speed < STATIONARY_SPEED:
        return 0.0, 0.0
    if status in MOORED_STATUSES and speed < MOORED_MAX_SPEED:
        return 0.0, 0.0
    return course, speed

def own_velocity(ship, smoother=None):
    """Own course and speed for CPA, stationary below STATIONARY_SPEED.

    With a smoother the velocity is averaged over OWN_SMOOTH_SECONDS and counts as
    stationary below OWN_STATIONARY_SPEED: a moored vessel's GPS shows 0.1-1.5 kn
    while the receiver keeps reporting its last real course, which would aim the
    own ship at its neighbours."""
    velocity = target_velocity(ship.get_cog(), ship.get_sog())
    if smoother is None:
        return velocity
    course, speed = smoother.update(*velocity)
    return (course, speed) if speed >= OWN_STATIONARY_SPEED else (0.0, 0.0)

class OwnVelocitySmoother:
    """Average the own velocity vector over the last seconds."""

    def __init__(self, window=OWN_SMOOTH_SECONDS, clock=now):
        self.window = window
        self.clock = clock
        self._history = collections.deque()

    def update(self, course, speed):
        """Add the current velocity and return the averaged (course, speed)."""
        at_time = self.clock()
        self._history.append((at_time, course, speed))
        while self._history[0][0] < at_time - self.window:
            self._history.popleft()
        east = sum(s * math.sin(math.radians(c)) for _, c, s in self._history)
        north = sum(s * math.cos(math.radians(c)) for _, c, s in self._history)
        count = len(self._history)
        return (math.degrees(math.atan2(east, north)) % 360,
                math.hypot(east, north) / count)

class ManoeuvreDetector:
    """Track whether the own vessel is manoeuvring: below MANOEUVRE_SPEED,
    until it makes MANOEUVRE_LEAVE_SPEED again."""

    def __init__(self):
        self.manoeuvring = False

    def update(self, sog):
        """Feed the own speed; return whether the vessel is manoeuvring."""
        if self.manoeuvring and sog >= MANOEUVRE_LEAVE_SPEED:
            log.warning("Own vessel under way; full warnings for all vessels")
            self.manoeuvring = False
        elif not self.manoeuvring and sog < MANOEUVRE_SPEED:
            log.warning("Own vessel manoeuvring; stationary vessels ignored")
            self.manoeuvring = True
        return self.manoeuvring

def track_profile(track: CPATrack, base: DangerProfile,
                  manoeuvring: bool) -> DangerProfile | None:
    """Danger profile for a track, or None when it is ignored.

    While the own vessel manoeuvres (mooring), moored and other stationary vessels
    are ignored and slow vessels get the harbour profile; vessels making
    MANOEUVRE_SPEED or more keep the base profile."""
    if not manoeuvring:
        return base
    if target_velocity(track.course, track.speed, track.status) == (0.0, 0.0):
        return None
    if track.speed < MANOEUVRE_SPEED:
        return HARBOUR_PROFILE
    return base

class RangeWatcher:
    """Measure how much each vessel's range really shrank over the last seconds."""

    def __init__(self, window=RANGE_WINDOW_SECONDS, min_closure_m=RANGE_CLOSURE_MIN_M):
        self.window = window
        self.min_closure_m = min_closure_m
        self._history = collections.defaultdict(collections.deque)

    def update(self, mmsi, at_time, distance_nm):
        """Record a range (NM) at at_time (seconds)."""
        if distance_nm is None:
            return
        history = self._history[mmsi]
        history.append((at_time, distance_nm))
        while len(history) > 1 and history[1][0] <= at_time - self.window:
            history.popleft()

    def stalled(self, mmsi, at_time):
        """Whether the range was observed for a full window without closing."""
        history = self._history.get(mmsi)
        if not history or at_time - history[0][0] < self.window:
            return False
        closure_m = (history[0][1] - history[-1][1]) * METERS_PER_NM
        return closure_m < self.min_closure_m

    def forget(self, mmsi):
        self._history.pop(mmsi, None)

class VelocitySmoother:
    """Average each vessel's last reported velocities as vectors.

    Reported course over ground jitters by degrees; averaging a few reports
    steadies the CPA for the small distances that matter in a river."""

    def __init__(self, max_reports=10):
        self._history = collections.defaultdict(
            lambda: collections.deque(maxlen=max_reports))

    def update(self, mmsi, course, speed, reports, status=None):
        """Add a report and return the averaged (course, speed)."""
        self._history[mmsi].append(target_velocity(course, speed, status))
        return self.get(mmsi, reports)

    def get(self, mmsi, reports):
        """Averaged (course, speed) over the last reports, or None when unknown."""
        history = list(self._history.get(mmsi, ()))[-reports:]
        if not history:
            return None
        east = sum(s * math.sin(math.radians(c)) for c, s in history) / len(history)
        north = sum(s * math.cos(math.radians(c)) for c, s in history) / len(history)
        return math.degrees(math.atan2(east, north)) % 360, math.hypot(east, north)

    def forget(self, mmsi):
        self._history.pop(mmsi, None)

def project_position(lat, lon, course, speed, seconds):
    """Dead-reckon a position (degrees) forward along course (degrees) at speed (knots)."""
    distance_nm = speed * seconds / 3600.0
    course = math.radians(course)
    lat2 = lat + distance_nm * math.cos(course) / 60.0
    lon2 = lon + distance_nm * math.sin(course) / (60.0 * math.cos(math.radians(lat)))
    return lat2, lon2

def refresh_cpa(tracker, known_ship, at_time, smoother=None, smooth_reports=None,
                own_smoother=None):
    """Recompute CPA data of all positioned tracks for the current own-ship state.

    Targets report only every few seconds to minutes (an anchored class A ship
    every 3 minutes) while the own ship keeps moving, so each target is projected
    from its last report to at_time. Returns the refreshed tracks."""
    refreshed = []
    for track in tracker.tracks:
        if track.position_time is None:
            continue
        velocity = None
        if smoother is not None:
            velocity = smoother.get(
                track.mmsi, smooth_reports or danger_profile().smooth_reports)
        course, speed = velocity or target_velocity(track.course, track.speed,
                                                    track.status)
        lat, lon = project_position(track.lat, track.lon, course, speed,
                                    max(0.0, at_time - track.position_time))
        result = cpa_tcpa(
            known_ship.get_mmsi(), known_ship.get_latitude(),
            known_ship.get_longitude(), *own_velocity(known_ship, own_smoother),
            track.mmsi, lat, lon, course, speed,
        )
        for key in CPA_KEYS:
            setattr(track, key, result[key])
        track.cpa_time = at_time
        refreshed.append(track)
    return refreshed

def track_messages(messages, que, known_ship=None, stop_event=None,
                   track_callback: Callable[[str, CPATrack], None] | None = None,
                   warning_enqueued=None):
    """Track decoded AIS messages, putting dangerous tracks on que.

    messages is an iterable of decoded pyais messages of the types in chain."""
    with CPATracker() as tracker:
        tracker.register_callback(AISTrackEvent.CREATED, handle_create)
        tracker.register_callback(AISTrackEvent.UPDATED, handle_update)
        tracker.register_callback(AISTrackEvent.DELETED, handle_delete)
        if track_callback is not None:
            tracker.register_callback(
                AISTrackEvent.DELETED,
                lambda track: track_callback("delete", track),
            )
        base_profile = danger_profile()
        manoeuvre = ManoeuvreDetector()
        manoeuvring = False
        smooth_reports = base_profile.smooth_reports
        smoother = VelocitySmoother()
        own_smoother = OwnVelocitySmoother()
        confirmations = {}  # mmsi -> consecutive dangerous position reports
        ranges = RangeWatcher()

        def dangerous(track, profile, at_time):
            ranges.update(track.mmsi, at_time, track.distance)
            if not dangerous_track(track, profile):
                if (track.tcpa is not None and 0 < track.tcpa < profile.tcpa
                        and (track.distance or 0) < profile.distance):
                    log.info("No warning for MMSI %s: clearance %.0f m, tcpa %.1f min,"
                             " distance %.0f m (limit %.0f m)", track.mmsi,
                             (hull_clearance(track) or 0) * METERS_PER_NM, track.tcpa,
                             track.distance * METERS_PER_NM, profile.cpa * METERS_PER_NM)
                return False
            if ranges.stalled(track.mmsi, at_time):
                log.info("No warning for MMSI %s: range not closing", track.mmsi)
                return False
            return True

        def forget(track):
            smoother.forget(track.mmsi)
            ranges.forget(track.mmsi)
            confirmations.pop(track.mmsi, None)

        def confirmed(track, profile):
            return confirmations.get(track.mmsi, 0) >= profile.confirm_reports

        tracker.register_callback(AISTrackEvent.DELETED, forget)

        def process(decoded):
            """Track one decoded message; warn when it shows a dangerous CPA."""
            cpa_res = { "cpa": None, "tcpa": None, "distance": None, "bearing": None, "approaching_speed": None }
            if decoded.msg_type in [1,2,3,18]:
                lat = available("lat", decoded.lat)
                lon = available("lon", decoded.lon)
                if lat is None or lon is None:
                    return
                if known_ship is not None:
                    course, speed = smoother.update(
                        decoded.mmsi, available("course", decoded.course),
                        available("speed", decoded.speed), smooth_reports,
                        getattr(decoded, "status", None))
                    cpa_res = cpa_tcpa(
                        known_ship.get_mmsi(), known_ship.get_latitude(),
                        known_ship.get_longitude(), *own_velocity(known_ship, own_smoother),
                        decoded.mmsi, lat, lon, course, speed,
                    )
                log.debug("MMSI %s CPA result: %s", decoded.mmsi, cpa_res)
                tracker.update(decoded, cpa_res)
                track = tracker.get_track(decoded.mmsi)
                profile = (track_profile(track, base_profile, manoeuvring)
                           if track is not None else None)
                if (known_ship is not None and profile is not None
                        and dangerous(track, profile, now())):
                    confirmations[track.mmsi] = confirmations.get(track.mmsi, 0) + 1
                    if que is not None and confirmed(track, profile):
                        queue_warning(que, track, warning_enqueued, profile)
                    log.info("Dangerous CPA detected for MMSI %s: %s", decoded.mmsi, cpa_res)
                elif track is not None:
                    confirmations.pop(track.mmsi, None)
            elif decoded.msg_type in [5,19,24]:
                tracker.update(decoded, cpa_res)
                log.debug("Updated non position message for MMSI %s", decoded.mmsi)
            else:
                return
            if track_callback is not None:
                track = tracker.get_track(decoded.mmsi)
                if track is not None:
                    track_callback("upsert", track)

        last_refresh = now()
        # None is a tick from with_ticks: no message, but time for periodic work
        for decoded in messages:
            if stop_event is not None and stop_event.is_set():
                break
            if known_ship is not None and settings.QUIET_MANOEUVRING:
                if manoeuvre.update(known_ship.get_sog()) != manoeuvring:
                    manoeuvring = manoeuvre.manoeuvring
                    confirmations.clear()
                    smooth_reports = (max(base_profile.smooth_reports,
                                          HARBOUR_PROFILE.smooth_reports)
                                      if manoeuvring else base_profile.smooth_reports)
            if decoded is not None:
                process(decoded)
            if known_ship is not None and now() - last_refresh >= CPA_REFRESH_SECONDS:
                last_refresh = now()
                for track in refresh_cpa(tracker, known_ship, last_refresh,
                                         smoother, smooth_reports, own_smoother):
                    profile = track_profile(track, base_profile, manoeuvring)
                    if profile is None or not dangerous(track, profile, last_refresh):
                        confirmations.pop(track.mmsi, None)
                    elif que is not None and confirmed(track, profile):
                        queue_warning(que, track, warning_enqueued, profile)
                    if track_callback is not None:
                        track_callback("upsert", track)
            if settings.VERBOSE:
                latest_tracks = tracker.n_latest_tracks(10)
                log.debug("Latest tracks: %s", latest_tracks)
                live_print(latest_tracks)

class _ReaderFailed:
    """An exception raised in the reader thread, handed to the consumer."""

    def __init__(self, error):
        self.error = error


_READER_DONE = object()


def with_ticks(iterable, interval=TICK_SECONDS):
    """Yield the items of iterable, and None whenever interval seconds pass
    without one, so the caller can do periodic work while the stream is quiet.

    The iterable is read in a daemon thread; an exception it raises is re-raised
    here, and its end ends this generator."""
    items = thread_queue.Queue()

    def pump():
        try:
            for item in iterable:
                items.put(item)
            items.put(_READER_DONE)
        except BaseException as error:  # handed to the consumer, raised there
            items.put(_ReaderFailed(error))

    threading.Thread(target=pump, name="ais-reader", daemon=True).start()
    while True:
        try:
            item = items.get(timeout=interval)
        except thread_queue.Empty:
            yield None
            continue
        if item is _READER_DONE:
            return
        if isinstance(item, _ReaderFailed):
            raise item.error
        yield item


def wake_reader(stream):
    """Shut the stream's socket down so a reader thread blocked in recv returns."""
    try:
        stream._fobj.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed or never connected


def do_track(que, known_ship=None, stop_event=None,
             track_callback: Callable[[str, CPATrack], None] | None = None,
             host=HOST, port=PORT, ais_seen=None):
    """Track one connection until the stream ends, fails or stop_event is set.

    KeyboardInterrupt propagates so the caller can shut down."""
    with open_stream(host, port) as ais_stream:
        try:
            messages = chain.start.filter(decoded_messages(ais_stream, ais_seen))
            track_messages(with_ticks(messages), que,
                           known_ship, stop_event, track_callback)
        finally:
            wake_reader(ais_stream)


def run_warning_process(que, warning_dequeued=None, speech_language=SPEECH_LANGUAGE):
    """Keep terminal interrupts in the parent so it can shut workers down together."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # The parent's SIGTERM handler is inherited; terminate() must just end this process.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    warn.do_warn(que, warning_dequeued, speech_language)


def start_warning_process(que, warning_dequeued=None,
                         speech_language=SPEECH_LANGUAGE):
    """Start a warning process reading que."""
    process = Process(
        target=run_warning_process,
        args=(que, warning_dequeued, speech_language),
        name="ais-warning",
    )
    process.start()
    return process


def supervise_warning_process(process, que, warning_dequeued=None,
                              speech_language=SPEECH_LANGUAGE):
    """Return a running warning process, restarting it if it has died.

    Without it, tracking would go on silently with no spoken warnings."""
    if process.is_alive():
        return process
    log.error("Warning process exited with code %s; restarting it", process.exitcode)
    process.join()
    free_reader_lock(que)
    return start_warning_process(que, warning_dequeued, speech_language)


def free_reader_lock(que):
    """Release the queue's reader lock if a dead reader left it held.

    A warning process killed inside que.get() (such as by the OOM killer while the
    TTS model fills memory) keeps the lock forever, and its replacement would block
    on it. The warning process is the only reader, so once it is dead the lock is
    free to release."""
    lock = getattr(que, "_rlock", None)
    if lock is None:
        return
    if lock.acquire(block=False):
        lock.release()
        return
    lock.release()
    log.error("Released the warning queue lock held by the dead warning process")


def warning_supervisor(que, stop_event, interval=RECONNECT_SECONDS,
                       speech_language=SPEECH_LANGUAGE):
    """Thread body: keep a warning process alive until stop_event is set.

    Returns a holder whose 'process' entry is the current warning process."""
    holder = {"process": start_warning_process(que, speech_language=speech_language)}

    def watch():
        while not stop_event.wait(interval):
            holder["process"] = supervise_warning_process(
                holder["process"], que, speech_language=speech_language
            )

    return holder, watch


def track_forever(que, known_ship, stop_event, host=HOST, port=PORT, ais_seen=None):
    """Run do_track, reconnecting after connection errors or end of stream."""
    while not stop_event.is_set():
        try:
            do_track(que, known_ship, stop_event, host=host, port=port,
                     ais_seen=ais_seen)
            if not stop_event.is_set():
                log.error("AIS stream %s:%s ended; reconnecting in %s s",
                          host, port, RECONNECT_SECONDS)
        except OSError as error:
            log.error("AIS stream %s:%s failed: %s; reconnecting in %s s",
                      host, port, error, RECONNECT_SECONDS)
        except Exception:  # a bug must not end collision warnings for good
            log.exception("AIS tracking failed; restarting in %s s",
                          RECONNECT_SECONDS)
        stop_event.wait(RECONNECT_SECONDS)


def add_mode_argument(parser):
    """Add --open and --river to an argument parser."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--open", dest="mode", action="store_const", const="open",
                       help="open water thresholds: CPA under 1 NM "
                            f"(default: {settings.NAVIGATION_MODE})")
    group.add_argument("--river", dest="mode", action="store_const", const="river",
                       help="river/channel thresholds: CPA under 50 m "
                            f"(default: {settings.NAVIGATION_MODE})")


def apply_mode_argument(args):
    """Select the profile given by --open or --river; check the mode."""
    if args.mode is not None:
        settings.NAVIGATION_MODE = args.mode
    profile = danger_profile()
    log.warning("Navigation mode %s: CPA < %.0f m, TCPA < %g min",
                settings.NAVIGATION_MODE, profile.cpa * METERS_PER_NM, profile.tcpa)


def add_speech_argument(parser):
    """Add the global spoken-language selection."""
    parser.add_argument(
        "--speech-language",
        choices=("en", "de"),
        default=SPEECH_LANGUAGE,
        help="announcement language and TTS engine: English Kokoro or German Piper "
             f"(default: {SPEECH_LANGUAGE})",
    )


def apply_speech_argument(args):
    """Validate and store the selected language for warning workers."""
    global SPEECH_LANGUAGE
    if args.speech_language not in ("en", "de"):
        raise ValueError("speech language must be 'en' or 'de'")
    SPEECH_LANGUAGE = args.speech_language
    log.warning("Speech language %s", SPEECH_LANGUAGE)


def setup_logging():
    """Log warnings and errors to a size-limited file beside this script (SD-card friendly)."""
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(processName)s %(name)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])


def interrupt_on_sigterm(signum, frame):
    """Turn SIGTERM (systemctl stop, kill) into the same clean shutdown as Ctrl-C."""
    raise KeyboardInterrupt


if __name__ == '__main__':
    import argparse

    signal.signal(signal.SIGTERM, interrupt_on_sigterm)
    parser = argparse.ArgumentParser(description=__doc__)
    add_mode_argument(parser)
    add_speech_argument(parser)
    cli_args = parser.parse_args()
    setup_logging()
    apply_mode_argument(cli_args)
    apply_speech_argument(cli_args)
    my_ship = Ship(settings.OWN_MMSI, settings.OWN_START_LAT,
                   settings.OWN_START_LON, 0.0, 0.0)
    warn_que = Queue(MAX_MESSAGES)
    stop = threading.Event()
    warning, watch_warning = warning_supervisor(
        warn_que, stop, speech_language=SPEECH_LANGUAGE
    )
    link_monitor = LinkMonitor(my_ship, warn_que)
    forced_warning_shutdown = False

    try:
        my_ship.start()
        link_monitor.start()
        threading.Thread(target=watch_warning, name="warning-supervisor",
                         daemon=True).start()
        log.info("My ship and warning process started")
        track_forever(warn_que, my_ship, stop, ais_seen=link_monitor.ais_seen)
    except KeyboardInterrupt:
        log.info("Shutdown requested")
    finally:
        stop.set()
        link_monitor.stop()
        my_ship.stop()
        warning_process = warning["process"]

        if warning_process.is_alive():
            try:
                warn_que.put(None, timeout=5)
            except Full:
                log.warning("Warning queue remained full during shutdown")
                warning_process.terminate()
                forced_warning_shutdown = True
            warning_process.join(timeout=10)
            if warning_process.is_alive():
                warning_process.terminate()
                warning_process.join()
                forced_warning_shutdown = True
        else:
            warning_process.join()

        if forced_warning_shutdown:
            warn_que.cancel_join_thread()
        warn_que.close()
        if not forced_warning_shutdown:
            warn_que.join_thread()
