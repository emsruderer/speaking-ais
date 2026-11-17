"""Own-ship dead reckoning from heading, speed through water, leeway and current.

Velocities are complex numbers in knots, east + 1j * north; positions are
nautical miles on a local flat grid. While GPS is present the model learns

    ground velocity = calibration * water velocity + current

from 10 s segments: the GNSS displacement compared with the displacement of the
water track (heading, speed through water and leeway). The complex calibration
holds the log scale error (its length) and the heading offset (its angle) and
needs course changes to be learned; without them it stays 1. The current is the
mean of the remaining difference over the last CURRENT_WINDOW_SECONDS.

When GPS is lost, ground_velocity() gives the best available velocity:
  'water+current'  heading and speed through water, calibrated, plus the current
  'water'          the same without a current estimate yet
  'heading'        no speed through water: the last GPS ground vector, rotated by
                   the heading change since then
  None             no heading: the caller keeps the last course and speed
"""

import cmath
import collections
import math
import time

import settings

SENSOR_STALE_SECONDS = 5.0  # an older heading, speed or heel is not used
SEGMENT_SECONDS = 10.0
MAX_SEGMENT_SECONDS = 30.0  # a longer gap between fixes is no segment
# A segment with a larger heading change is not learned from, nor are the
# segments just before and after it: sensor latencies (GNSS lags the compass,
# or the other way round) would bias the calibration in turns.
MAX_SEGMENT_TURN = 15.0  # degrees
CURRENT_WINDOW_SECONDS = 180.0
MIN_CURRENT_SECONDS = 60.0
CURRENT_MAX_AGE_SECONDS = 3600.0  # tidal current is stale after an hour
CALIBRATION_TAU_SECONDS = 1800.0  # memory of the calibration sums
# Weight (knots squared times segments) that holds the calibration at 'no
# correction' until course and speed changes make it observable.
CALIBRATION_PRIOR = 200.0
MAX_SCALE_ERROR = 0.2  # log scale factor stays within 0.8 .. 1.2
MAX_HEADING_OFFSET = 10.0  # degrees
MAX_LEEWAY = 15.0  # degrees
MIN_LEEWAY_STW = 1.0  # knots; leeway formula is meaningless when slower
NM_PER_DEGREE = 60.0


def vector(course, speed):
    """Velocity vector (east + 1j * north) for a compass course and speed."""
    angle = math.radians(course)
    return speed * complex(math.sin(angle), math.cos(angle))


def course_speed(velocity):
    """Compass course (degrees) and speed of a velocity vector."""
    return math.degrees(math.atan2(velocity.real, velocity.imag)) % 360.0, abs(velocity)


def rotate(velocity, degrees_clockwise):
    """Rotate a vector clockwise, as a course change to starboard does."""
    return velocity * cmath.exp(-1j * math.radians(degrees_clockwise))


def leeway(heel, stw, coefficient):
    """Leeway angle in degrees (positive to starboard) from heel and speed."""
    if not coefficient or heel is None or stw is None or stw < MIN_LEEWAY_STW:
        return 0.0
    return max(-MAX_LEEWAY, min(MAX_LEEWAY, coefficient * heel / stw ** 2))


def heading_offset(calibration):
    """Heading correction in degrees (clockwise) held by a calibration factor."""
    return -math.degrees(cmath.phase(calibration))


def _limit(calibration):
    scale = min(max(abs(calibration), 1 - MAX_SCALE_ERROR), 1 + MAX_SCALE_ERROR)
    offset = min(max(heading_offset(calibration), -MAX_HEADING_OFFSET), MAX_HEADING_OFFSET)
    return scale * cmath.exp(-1j * math.radians(offset))


class DeadReckoning:
    """Learn calibration and current while GPS is present; model velocity without it."""

    def __init__(self, clock=time.monotonic, leeway_coefficient=None, variation=None):
        self.clock = clock
        self.leeway_coefficient = (settings.LEEWAY_COEFFICIENT
                                   if leeway_coefficient is None else leeway_coefficient)
        self.variation = settings.MAGNETIC_VARIATION if variation is None else variation
        self._heading = None  # (time, true heading)
        self._stw = None  # (time, knots)
        self._heel = None  # (time, degrees, starboard down positive)
        self._ground = None  # (time, last GPS ground vector, true heading then)
        self._water = 0j  # integrated water track (NM), uncorrected
        self._water_time = None
        self._water_gap = False  # water track incomplete since the segment start
        self._reference_lat = None
        self._segment = None  # (time, GNSS position, water position, heading)
        self._pending = None  # last segment, learned once the next one shows no turn
        self._after_turn = False
        self._segments = collections.deque()  # (time, ground, water) velocities
        self._sums = [0.0, 0j, 0.0, 0j, 0j]  # weight, W, |W|^2, G, G*conj(W)
        self._sums_time = None
        self.calibration = 1 + 0j
        self.current = None
        self.current_time = None

    # sensor input --------------------------------------------------------

    def update_heading(self, true_heading=None, magnetic_heading=None,
                       sentence_variation=None):
        """Record a true heading, or a magnetic one converted with the variation."""
        if true_heading is None and magnetic_heading is not None:
            variation = self.variation
            if variation is None:
                variation = sentence_variation or 0.0
            true_heading = magnetic_heading + variation
        if true_heading is None:
            return
        now = self._integrate()
        self._heading = (now, true_heading % 360.0)

    def update_stw(self, knots):
        """Record the speed through water."""
        if knots is None or knots < 0:
            return
        now = self._integrate()
        self._stw = (now, knots)

    def update_heel(self, degrees):
        """Record the heel angle, starboard side down positive."""
        if degrees is None:
            return
        now = self._integrate()
        self._heel = (now, degrees)

    def update_ground(self, cog, sog):
        """Record the GPS course and speed over ground with the heading at that time."""
        if cog is None or sog is None:
            return
        now = self.clock()
        self._ground = (now, vector(cog, sog), self._fresh(self._heading, now))

    def update_fix(self, lat, lon):
        """Record a GNSS position; completes a learning segment every SEGMENT_SECONDS."""
        now = self._integrate()
        if self._reference_lat is None:
            self._reference_lat = lat
        position = complex(lon * NM_PER_DEGREE * math.cos(math.radians(self._reference_lat)),
                           lat * NM_PER_DEGREE)
        heading = self._fresh(self._heading, now)
        if self._segment is not None and not self._water_gap:
            start, start_position, start_water, start_heading = self._segment
            seconds = now - start
            if seconds < SEGMENT_SECONDS:
                return
            turn = (abs((heading - start_heading + 180.0) % 360.0 - 180.0)
                    if heading is not None and start_heading is not None else 0.0)
            if seconds > MAX_SEGMENT_SECONDS or turn > MAX_SEGMENT_TURN:
                self._pending = None  # the segment before a turn
                self._after_turn = turn > MAX_SEGMENT_TURN
            elif self._after_turn:
                self._after_turn = False  # the segment after a turn
            else:
                hours = seconds / 3600.0
                if self._pending is not None:
                    self._learn(*self._pending)
                self._pending = (now, (position - start_position) / hours,
                                 (self._water - start_water) / hours)
        else:
            self._pending = None
        self._segment = (now, position, self._water, heading)
        self._water_gap = False

    # model ---------------------------------------------------------------

    def water_velocity(self, at_time=None):
        """Uncorrected water-track vector from fresh heading and speed, or None."""
        at_time = self.clock() if at_time is None else at_time
        heading = self._fresh(self._heading, at_time)
        stw = self._fresh(self._stw, at_time)
        if heading is None or stw is None:
            return None
        heel = self._fresh(self._heel, at_time)
        return vector(heading + leeway(heel, stw, self.leeway_coefficient), stw)

    def ground_velocity(self, at_time=None):
        """Best modelled velocity over ground and its source name, or (None, None)."""
        at_time = self.clock() if at_time is None else at_time
        water = self.water_velocity(at_time)
        if water is not None:
            velocity = self.calibration * water
            if (self.current is not None
                    and at_time - self.current_time <= CURRENT_MAX_AGE_SECONDS):
                return velocity + self.current, "water+current"
            return velocity, "water"
        heading = self._fresh(self._heading, at_time)
        if heading is not None and self._ground is not None and self._ground[2] is not None:
            _, ground, heading_then = self._ground
            return rotate(ground, heading - heading_then), "heading"
        return None, None

    def describe(self):
        """Learned values for the log."""
        current = ("none" if self.current is None else
                   "%.0f deg %.2f kn" % course_speed(self.current))
        return (f"log scale {abs(self.calibration):.3f}, heading offset "
                f"{heading_offset(self.calibration):+.1f} deg, current {current}")

    # internals -----------------------------------------------------------

    @staticmethod
    def _fresh(reading, at_time):
        if reading is None or at_time - reading[0] > SENSOR_STALE_SECONDS:
            return None
        return reading[1]

    def _integrate(self):
        """Advance the water track to now with the readings valid until now."""
        now = self.clock()
        if self._water_time is not None:
            water = self.water_velocity(self._water_time)
            if water is None:
                self._water_gap = True
            else:
                self._water += water * (now - self._water_time) / 3600.0
        self._water_time = now
        return now

    def _learn(self, now, ground, water):
        """Add one segment (velocities in knots) and update calibration and current."""
        if self._sums_time is not None:
            decay = math.exp(-(now - self._sums_time) / CALIBRATION_TAU_SECONDS)
            self._sums = [value * decay for value in self._sums]
        self._sums_time = now
        weight, s_w, s_ww, s_g, s_gw = self._sums
        weight += 1.0
        s_w += water
        s_ww += abs(water) ** 2
        s_g += ground
        s_gw += ground * water.conjugate()
        self._sums = [weight, s_w, s_ww, s_g, s_gw]
        # Least squares for ground = a * water + c with a pulled towards 1:
        #   a (s_ww + prior) + c conj(s_w) = s_gw + prior,   a s_w + c weight = s_g
        denominator = s_ww + CALIBRATION_PRIOR - abs(s_w) ** 2 / weight
        numerator = s_gw + CALIBRATION_PRIOR - s_w.conjugate() * s_g / weight
        self.calibration = _limit(numerator / denominator)

        self._segments.append((now, ground, water))
        while self._segments and self._segments[0][0] <= now - CURRENT_WINDOW_SECONDS:
            self._segments.popleft()
        if len(self._segments) * SEGMENT_SECONDS >= MIN_CURRENT_SECONDS:
            residuals = [g - self.calibration * w for _, g, w in self._segments]
            self.current = sum(residuals) / len(residuals)
            self.current_time = now
