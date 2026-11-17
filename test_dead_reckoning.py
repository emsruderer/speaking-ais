"""Tests for the dead-reckoning model, driven by a simulated boat.

Positions lie at buoy MN1, Maasmond (Rotterdam): 52.037 N, 4.032 E."""

import math
import random
import unittest
from unittest.mock import patch

import settings
import ships
from dead_reckoning import (DeadReckoning, course_speed, heading_offset, leeway,
                            vector)
from gps_stream import decode_line, decode_vhw
from ships import Ship

LAT0, LON0 = 52.037, 4.032
METERS_PER_NM = 1852.0


class SimulatedBoat:
    """A boat with true motion, a current and imperfect sensors.

    The compass reads compass_bias degrees too high and the log reads
    log_factor times the true speed through water."""

    def __init__(self, heading=0.0, stw=6.0, current=(0.0, 0.0), compass_bias=0.0,
                 log_factor=1.0, gps_noise_m=3.0, seed=1, compass_lag_seconds=0):
        self.clock = [1000.0]
        self.model = DeadReckoning(clock=lambda: self.clock[0],
                                   leeway_coefficient=0.0, variation=0.0)
        self.heading, self.stw = heading, stw
        self.current = vector(*current)
        self.compass_bias, self.log_factor = compass_bias, log_factor
        self.gps_noise_nm = gps_noise_m / METERS_PER_NM
        self.random = random.Random(seed)
        self.position = 0j  # true position, NM east + 1j * NM north of MN1
        self.headings = [heading] * (compass_lag_seconds + 1)  # compass delay line

    def ground(self):
        return vector(self.heading, self.stw) + self.current

    def latlon(self, position):
        return (LAT0 + position.imag / 60.0,
                LON0 + position.real / (60.0 * math.cos(math.radians(LAT0))))

    def run(self, seconds, gps=True, log=True, compass=True):
        for _ in range(int(seconds)):
            # sensors report the motion of the coming second, then the boat moves
            self.headings = self.headings[1:] + [self.heading]
            if compass:
                self.model.update_heading(true_heading=self.headings[0] + self.compass_bias
                                          + self.random.gauss(0, 1.0))
            if log:
                self.model.update_stw(self.stw * self.log_factor
                                      + self.random.gauss(0, 0.05))
            self.clock[0] += 1.0
            self.position += self.ground() / 3600.0
            if gps:
                noise = complex(self.random.gauss(0, self.gps_noise_nm),
                                self.random.gauss(0, self.gps_noise_nm))
                self.model.update_ground(*course_speed(self.ground()))
                self.model.update_fix(*self.latlon(self.position + noise))

    def zigzag(self, minutes, courses=(0, 90, 180, 270)):
        for minute in range(0, minutes, 2):
            self.heading = courses[(minute // 2) % len(courses)]
            self.run(120)


def outage_errors(boat, seconds, turn_after=None, turn_to=None):
    """Sail an outage; return (model error, simple error) in meters.

    The simple method keeps the last GPS course and speed."""
    model_position = simple_position = boat.position
    simple_velocity = boat.ground()
    for second in range(int(seconds)):
        if turn_after is not None and second == turn_after:
            boat.heading = turn_to
        boat.run(1, gps=False)
        velocity, _ = boat.model.ground_velocity()
        model_position += velocity / 3600.0
        simple_position += simple_velocity / 3600.0
    return (abs(model_position - boat.position) * METERS_PER_NM,
            abs(simple_position - boat.position) * METERS_PER_NM)


class LearningTests(unittest.TestCase):
    def test_learns_current_on_a_steady_course(self):
        boat = SimulatedBoat(heading=0, stw=6, current=(90, 1.5))
        boat.run(300)
        course, speed = course_speed(boat.model.current)
        # 3 m GPS noise over the 180 s window: about 0.05 kn standard deviation
        self.assertAlmostEqual(speed, 1.5, delta=0.1)
        self.assertAlmostEqual(course, 90, delta=4)
        self.assertEqual(boat.model.ground_velocity()[1], "water+current")
        # one sample carries the compass noise; ais_stream averages over 30 s
        samples = []
        for _ in range(30):
            boat.run(1, gps=False)
            samples.append(boat.model.ground_velocity()[0])
        self.assertLess(abs(sum(samples) / len(samples) - boat.ground()), 0.15)

    def test_steady_course_leaves_calibration_alone(self):
        # Without course changes a compass error cannot be told from current.
        boat = SimulatedBoat(heading=45, compass_bias=4, current=(200, 1.0))
        boat.run(1200)
        self.assertLess(abs(heading_offset(boat.model.calibration)), 1.0)
        self.assertLess(abs(boat.model.ground_velocity()[0] - boat.ground()), 0.15)

    def test_course_changes_reveal_compass_and_log_errors(self):
        boat = SimulatedBoat(compass_bias=4, log_factor=1.05, current=(200, 1.0))
        boat.zigzag(40)
        self.assertAlmostEqual(heading_offset(boat.model.calibration), -4, delta=0.5)
        self.assertAlmostEqual(abs(boat.model.calibration), 1 / 1.05, delta=0.01)
        course, speed = course_speed(boat.model.current)
        self.assertAlmostEqual(speed, 1.0, delta=0.1)
        self.assertAlmostEqual(course, 200, delta=6)

    def test_compass_latency_does_not_bias_calibration(self):
        boat = SimulatedBoat(compass_bias=4, log_factor=1.05, current=(200, 1.0),
                             compass_lag_seconds=2)
        boat.zigzag(40)
        self.assertAlmostEqual(heading_offset(boat.model.calibration), -4, delta=0.5)

    def test_no_learning_without_log(self):
        boat = SimulatedBoat(current=(90, 1.0))
        boat.run(300, log=False)
        self.assertIsNone(boat.model.current)


class OutageTests(unittest.TestCase):
    def learned_boat(self):
        boat = SimulatedBoat(compass_bias=3, log_factor=0.97, current=(120, 1.2))
        boat.zigzag(40)
        boat.heading = 0
        boat.run(240)
        return boat

    def test_course_change_during_outage(self):
        boat = self.learned_boat()
        model_error, simple_error = outage_errors(boat, 600, turn_after=120, turn_to=90)
        self.assertLess(model_error, 60)
        self.assertGreater(simple_error, 1000)

    def test_steady_course_outage_is_no_worse_than_simple(self):
        boat = self.learned_boat()
        model_error, simple_error = outage_errors(boat, 600)
        self.assertLess(model_error, 60)
        self.assertLess(model_error, simple_error + 30)

    def test_heading_only_rotates_last_ground_vector(self):
        boat = SimulatedBoat(heading=0, stw=5, current=(90, 1.0))
        boat.run(60, log=False)
        boat.heading = 90
        boat.run(10, gps=False, log=False)
        velocity, source = boat.model.ground_velocity()
        self.assertEqual(source, "heading")
        # last ground vector (5 kn north + 1 kn east) turned 90 degrees to starboard
        expected = vector(*course_speed(vector(0, 5) + vector(90, 1.0)))
        expected_course = (course_speed(expected)[0] + 90) % 360
        self.assertAlmostEqual(course_speed(velocity)[0], expected_course, delta=3)
        self.assertAlmostEqual(abs(velocity), abs(expected), delta=0.01)

    def test_no_heading_gives_no_model(self):
        boat = SimulatedBoat()
        boat.run(60)
        boat.run(10, gps=False, compass=False)
        self.assertEqual(boat.model.ground_velocity(), (None, None))

    def test_old_current_is_not_used(self):
        boat = SimulatedBoat(current=(90, 1.0))
        boat.run(300)
        boat.run(3700, gps=False)
        self.assertEqual(boat.model.ground_velocity()[1], "water")


class SensorTests(unittest.TestCase):
    def test_leeway(self):
        self.assertAlmostEqual(leeway(10, 5, 10), 4.0)  # starboard down: to starboard
        self.assertAlmostEqual(leeway(-10, 5, 10), -4.0)
        self.assertEqual(leeway(10, 0.5, 10), 0.0)  # too slow
        self.assertEqual(leeway(30, 1.0, 10), 15.0)  # limited
        self.assertEqual(leeway(10, 5, 0), 0.0)  # disabled

    def test_magnetic_heading_uses_variation_setting_first(self):
        clock = [0.0]
        model = DeadReckoning(clock=lambda: clock[0], variation=2.5)
        model.update_heading(magnetic_heading=100, sentence_variation=0.0)
        model.update_stw(5)
        self.assertAlmostEqual(course_speed(model.water_velocity())[0], 102.5)

    def test_magnetic_heading_falls_back_to_sentence_variation(self):
        with patch.object(settings, "MAGNETIC_VARIATION", None):
            model = DeadReckoning(clock=lambda: 0.0)
        model.update_heading(magnetic_heading=100, sentence_variation=-1.5)
        model.update_stw(5)
        self.assertAlmostEqual(course_speed(model.water_velocity())[0], 98.5)

    def test_stale_sensors_are_ignored(self):
        clock = [0.0]
        model = DeadReckoning(clock=lambda: clock[0])
        model.update_heading(true_heading=10)
        model.update_stw(5)
        clock[0] = 6.0
        self.assertIsNone(model.water_velocity())

    def test_ship_feeds_hdg_vhw_and_heel_to_model(self):
        clock = [0.0]
        with patch("ships.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(settings, "MAGNETIC_VARIATION", 2.0):
            ship = Ship(123456789, LAT0, LON0, 0.0, 0.0)
            # sentence as received from the boat's SignalK on 2026-11-17
            for line in ("$IIHDG,350.57,,,0.00,E*26", sentence("IIVHW,,T,,M,5.0,N,,K"),
                         sentence("IIXDR,A,12.0,D,ROLL")):
                ship._apply_navigation(decode_line(line))
            model = ship.model
            self.assertAlmostEqual(model._heading[1], 352.57)
            self.assertEqual(model._stw[1], 5.0)
            self.assertEqual(model._heel[1], 12.0)

    def test_decode_vhw(self):
        self.assertEqual(decode_vhw("IIVHW,,T,,M,5.4,N,10.0,K".split(","))
                         ["speed_through_water"], 5.4)
        self.assertAlmostEqual(decode_vhw("IIVHW,,T,,M,,N,10.0,K".split(","))
                               ["speed_through_water"], 10 / 1.852)
        self.assertIsNone(decode_vhw("IIVHW,,T,,M,,N,,K".split(","))
                          ["speed_through_water"])


def sentence(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}"


def rmc(lat, lon, cog, sog):
    def dm(value, digits, hemis):
        hemi = hemis[0] if value >= 0 else hemis[1]
        value = abs(value)
        degrees = int(value)
        return f"{degrees:0{digits}d}{(value - degrees) * 60:09.6f}", hemi
    lat_text, ns = dm(lat, 2, "NS")
    lon_text, ew = dm(lon, 3, "EW")
    return sentence(f"GNRMC,120000.000,A,{lat_text},{ns},{lon_text},{ew},"
                    f"{sog:.2f},{cog:.2f},171126,,,D,V")


class ShipIntegrationTests(unittest.TestCase):
    """Sentences through Ship: learning while GPS is present, then an outage."""

    def sail(self, use_sensors):
        clock = [1000.0]
        boat = SimulatedBoat(heading=0, stw=6, current=(90, 1.0), gps_noise_m=0)
        with patch("ships.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(settings, "DEAD_RECKONING_SENSORS", use_sensors), \
                patch.object(settings, "SET_SYSTEM_CLOCK", False):
            ship = Ship(123456789, LAT0, LON0, 0.0, 0.0)

            def second(gps):
                clock[0] += 1.0
                boat.position += boat.ground() / 3600.0
                lines = [sentence(f"IIHDT,{boat.heading:.1f},T"),
                         sentence(f"IIVHW,,T,,M,{boat.stw:.2f},N,,K")]
                if gps:
                    lines.append(rmc(*boat.latlon(boat.position), *course_speed(boat.ground())))
                for line in lines:
                    ship._apply_navigation(decode_line(line))
                ship._advance_dead_reckoning()

            for _ in range(300):
                second(gps=True)
            boat.heading = 90  # turn to starboard during the outage
            for _ in range(300):
                second(gps=False)
            published = (ship.get_latitude(), ship.get_longitude())
            truth = boat.latlon(boat.position)
            with self.assertLogs(ships.log, "WARNING") as logs:
                second(gps=True)
        return ships.distance_m(published, truth), ship, logs.output[0]

    def test_shadow_mode_publishes_simple_track_and_logs_both_errors(self):
        error, ship, report = self.sail(use_sensors=False)
        self.assertGreater(error, 500)  # simple track missed the turn
        self.assertIn("GPS back after 3", report)
        self.assertRegex(report, r"\d+ m keeping course and speed, \d+ m with the sensor model")
        self.assertIn("water+current", report)
        self.assertFalse(ship.deadreckoning)

    def test_sensor_mode_publishes_model_track(self):
        error, ship, _ = self.sail(use_sensors=True)
        self.assertLess(error, 40)


if __name__ == "__main__":
    unittest.main()
