"""Tests for AIS 'not available' handling and warning hand-off."""

import math
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import settings
from ais_stream import (HARBOUR_PROFILE, add_mode_argument, apply_mode_argument, ManoeuvreDetector, VelocitySmoother, chain,
                        danger_profile, dangerous_track, decoded_messages,
                        hull_clearance, own_velocity, project_position,
                        queue_warning, refresh_cpa, target_velocity,
                        track_messages)
from pyais.stream import IterMessages
from cpa_tracker import CPATrack, CPATracker, available
from nationality import get_country


class AvailableTests(unittest.TestCase):
    def test_not_available_values_become_none(self):
        for name, value in (("lat", 91.0), ("lon", 181.0), ("speed", 102.3),
                            ("course", 360.0), ("heading", 511)):
            self.assertIsNone(available(name, value), name)

    def test_valid_values_pass_through(self):
        for name, value in (("lat", -53.2), ("lon", 7.4), ("speed", 0.0),
                            ("course", 359.9), ("heading", 0), ("shipname", "X")):
            self.assertEqual(available(name, value), value, name)

    def test_tracker_does_not_store_not_available_values(self):
        tracker = CPATracker()
        msg = SimpleNamespace(mmsi=244000001, lat=53.2, lon=7.4,
                              speed=102.3, course=360.0, heading=511)
        tracker.update(msg, None)
        track = tracker.get_track(244000001)
        self.assertEqual((track.lat, track.lon), (53.2, 7.4))
        self.assertIsNone(track.speed)
        self.assertIsNone(track.course)
        self.assertIsNone(track.heading)


class QueueWarningTests(unittest.TestCase):
    def test_queues_a_copy(self):
        que = queue.Queue(1)
        track = CPATrack(mmsi=1, cpa=0.5)
        queue_warning(que, track)
        track.cpa = 9.0
        self.assertEqual(que.get_nowait().cpa, 0.5)

    def test_full_queue_drops_instead_of_blocking(self):
        que = queue.Queue(1)
        queue_warning(que, CPATrack(mmsi=1))
        with self.assertLogs("ais_stream", "WARNING"):
            queue_warning(que, CPATrack(mmsi=2))
        self.assertEqual(que.get_nowait().mmsi, 1)


class OwnShip:
    """Own ship at 53N 7E heading north at 6 kn."""
    lat, lon = 53.0, 7.0

    def get_mmsi(self):
        return "244030153"

    def get_latitude(self):
        return self.lat

    def get_longitude(self):
        return self.lon

    def get_cog(self):
        return 0.0

    def get_sog(self):
        return 6.0

    def get_position(self):
        return self.lat, self.lon


class RefreshCpaTests(unittest.TestCase):
    def test_static_message_does_not_set_position_time(self):
        tracker = CPATracker(ttl_in_seconds=None)
        tracker.update(SimpleNamespace(mmsi=1, shipname="X"), None, ts_epoch_ms=50.0)
        self.assertIsNone(tracker.get_track(1).position_time)
        tracker.update(SimpleNamespace(mmsi=1, lat=53.0, lon=7.0), None, ts_epoch_ms=60.0)
        tracker.update(SimpleNamespace(mmsi=1, shipname="X"), None, ts_epoch_ms=70.0)
        self.assertEqual(tracker.get_track(1).position_time, 60.0)

    def test_project_position(self):
        lat, lon = project_position(53.0, 7.0, 90.0, 6.0, 3600)
        self.assertAlmostEqual(lat, 53.0)
        self.assertAlmostEqual(lon, 7.0 + 6.0 / (60.0 * 0.6018150232), places=6)

    def test_refresh_uses_current_own_position_and_projected_target(self):
        ship = OwnShip()
        tracker = CPATracker(ttl_in_seconds=None)
        # anchored target 2 NM ahead, last reported at t=0
        tracker.update(SimpleNamespace(mmsi=1, lat=53.0 + 2 / 60, lon=7.0,
                                       speed=0.0, course=0.0), None, ts_epoch_ms=0.0)
        [track] = refresh_cpa(tracker, ship, 0.0)
        self.assertAlmostEqual(track.distance, 2.0, places=3)
        self.assertAlmostEqual(track.tcpa, 20.0, places=2)

        ship.lat = 53.0 + 1 / 60  # own ship has moved 1 NM closer
        [track] = refresh_cpa(tracker, ship, 600.0)
        self.assertAlmostEqual(track.distance, 1.0, places=3)
        self.assertAlmostEqual(track.tcpa, 10.0, places=2)

    def test_refresh_projects_a_moving_target(self):
        tracker = CPATracker(ttl_in_seconds=None)
        # target 2 NM ahead heading south at 6 kn, reported 10 minutes ago
        tracker.update(SimpleNamespace(mmsi=1, lat=53.0 + 2 / 60, lon=7.0,
                                       speed=6.0, course=180.0), None, ts_epoch_ms=0.0)
        [track] = refresh_cpa(tracker, OwnShip(), 600.0)
        self.assertAlmostEqual(track.distance, 1.0, places=3)


class RangeFilterTests(unittest.TestCase):
    def test_tracking_honors_configured_radius_and_none_disables_it(self):
        reports = [report(0, distance_nm=3.0, mmsi=1),
                   report(0, distance_nm=15.0, mmsi=2)]
        own_ship = OwnShip()
        own_ship.lat = 53.26
        own_ship.lon = 7.39

        def tracked_mmsis(radius):
            updates = []

            def collect_track(_action, tracked):
                updates.append(tracked.mmsi)

            with patch.object(settings, "AIS_RANGE_NM", radius):
                track_messages(reports, None, own_ship,
                               track_callback=collect_track)
            return set(updates)

        self.assertEqual(tracked_mmsis(12.0), {1})
        self.assertEqual(tracked_mmsis(None), {1, 2})


class NationalityTests(unittest.TestCase):
    def test_falkland_islands(self):
        self.assertEqual(get_country(740123456), ("FK", "Falkland Islands"))


class RobustnessTests(unittest.TestCase):
    def test_system_clock_step_back_does_not_break_tracking(self):
        tracker = CPATracker()
        msg = SimpleNamespace(mmsi=1, lat=53.2, lon=7.4)
        tracker.update(msg, None)
        with patch("time.time", return_value=0.0):  # wall clock jumps to 1970
            tracker.update(msg, None)
        self.assertIsNotNone(tracker.get_track(1))

    def test_undecodable_messages_are_skipped(self):
        lines = [b"!AIVDM,1,1,,A,O,0*69",  # valid checksum, unsupported type
                 b"!AIVDM,1,1,,B,15NG6V0P01G?cFhE`R2IU?wn28R>,0*05"]
        with self.assertLogs("ais_stream", "WARNING"):
            decoded = list(chain.start.filter(decoded_messages(IterMessages(lines))))
        self.assertEqual([msg.mmsi for msg in decoded], [367380120])

class DangerRuleTests(unittest.TestCase):
    """Open water thresholds."""

    def setUp(self):
        patcher = patch.object(settings, "NAVIGATION_MODE", "open")
        patcher.start()
        self.addCleanup(patcher.stop)

    def track(self, **fields):
        values = dict(mmsi=1, cpa=0.5, tcpa=8.0, distance=3.0, speed=12.0, msg_type=1)
        values.update(fields)
        return CPATrack(**values)

    def test_fast_vessel_beyond_five_miles_warned_within_ten_minutes(self):
        self.assertTrue(dangerous_track(self.track(distance=6.8, tcpa=9.9, speed=35.0)))
        self.assertFalse(dangerous_track(self.track(distance=12.0, tcpa=17.6, speed=35.0)))

    def test_last_seconds_before_cpa_are_dangerous(self):
        self.assertTrue(dangerous_track(self.track(tcpa=0.2, distance=0.1)))
        self.assertFalse(dangerous_track(self.track(tcpa=-0.1, distance=0.1)))

    def test_slow_class_b_is_relaxed(self):
        for msg_type in (18, 19, 24):
            self.assertFalse(dangerous_track(self.track(msg_type=msg_type, speed=7.0)))
            self.assertTrue(dangerous_track(
                self.track(msg_type=msg_type, speed=7.0, cpa=0.2, tcpa=5.0)))
            self.assertFalse(dangerous_track(
                self.track(msg_type=msg_type, speed=7.0, cpa=0.2, tcpa=12.0)))

    def test_fast_or_unknown_speed_class_b_and_slow_class_a_use_normal_rules(self):
        self.assertTrue(dangerous_track(self.track(msg_type=18, speed=7.1)))
        self.assertTrue(dangerous_track(self.track(msg_type=18, speed=None)))
        self.assertTrue(dangerous_track(self.track(msg_type=1, speed=3.0)))

    def test_tracker_keeps_message_type_and_cpa_time(self):
        tracker = CPATracker()
        tracker.update(SimpleNamespace(mmsi=1, msg_type=18, lat=53.2, lon=7.4),
                       {"cpa": 0.1, "tcpa": 5.0, "distance": 1.0, "bearing": 0.0,
                        "approaching_speed": 10.0, "rel_east": 0.0, "rel_north": -0.2})
        track = tracker.get_track(1)
        self.assertEqual(track.msg_type, 18)
        self.assertEqual(track.cpa_time, track.last_updated)
        self.assertEqual(track.rel_north, -0.2)

class RiverOwnShip:
    def get_mmsi(self): return "244030153"
    def get_latitude(self): return 53.26
    def get_longitude(self): return 7.39
    def get_cog(self): return 0.0
    def get_sog(self): return 6.0


def report(sideways_m, distance_nm=0.3, course=180.0, speed=8.0, mmsi=2):
    """Position report of a vessel ahead, offset sideways (east) by sideways_m."""
    import math
    return SimpleNamespace(
        msg_type=1, mmsi=mmsi, lat=53.26 + distance_nm / 60,
        lon=7.39 + sideways_m / 1852 / 60 / math.cos(math.radians(53.26)),
        course=course, speed=speed, heading=None)


class RiverModeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(settings, "NAVIGATION_MODE", "river")
        patcher.start()
        self.addCleanup(patcher.stop)

    def track(self, cpa_m, distance_nm, tcpa=2.0, **fields):
        values = dict(mmsi=1, cpa=cpa_m / 1852, tcpa=tcpa, distance=distance_nm,
                      speed=8.0, msg_type=1)
        values.update(fields)
        return CPATrack(**values)

    def test_only_close_cpa_within_half_a_mile(self):
        self.assertTrue(dangerous_track(self.track(40, 0.4)))
        self.assertFalse(dangerous_track(self.track(70, 0.4)))   # normal passing
        self.assertFalse(dangerous_track(self.track(40, 0.6)))   # too far to trust
        self.assertFalse(dangerous_track(self.track(0, 2.0, tcpa=3.0, speed=40.0)))

    def test_slow_class_b_needs_25_m(self):
        self.assertFalse(dangerous_track(self.track(40, 0.3, msg_type=18, speed=5.0)))
        self.assertTrue(dangerous_track(self.track(20, 0.3, msg_type=18, speed=5.0)))

    def test_unknown_mode_is_rejected(self):
        with patch.object(settings, "NAVIGATION_MODE", "lake"), \
                self.assertRaises(ValueError):
            danger_profile()

    def run_reports(self, *reports):
        que = queue.Queue()
        track_messages(list(reports), que, RiverOwnShip())
        return que.qsize()

    def test_warns_after_two_consecutive_dangerous_reports(self):
        self.assertEqual(self.run_reports(report(30)), 0)
        self.assertEqual(self.run_reports(report(30), report(30, 0.29)), 1)

    def test_safe_report_resets_confirmation(self):
        self.assertEqual(self.run_reports(report(30), report(300, 0.29),
                                          report(30, 0.28)), 0)

    def test_normal_passing_is_quiet(self):
        self.assertEqual(self.run_reports(*(report(70, 0.3 - i * 0.01)
                                            for i in range(10))), 0)

    def test_open_mode_warns_on_first_report(self):
        with patch.object(settings, "NAVIGATION_MODE", "open"):
            self.assertEqual(self.run_reports(report(70)), 1)


class VelocityTests(unittest.TestCase):
    def test_average_wraps_around_north(self):
        smoother = VelocitySmoother()
        smoother.update(1, 358.0, 10.0, 5)
        course, speed = smoother.update(1, 2.0, 10.0, 5)
        self.assertAlmostEqual(min(course, 360 - course), 0.0, places=6)
        self.assertAlmostEqual(speed, 10.0 * math.cos(math.radians(2)), places=6)

    def test_only_last_reports_count(self):
        smoother = VelocitySmoother()
        smoother.update(1, 90.0, 10.0, 2)
        smoother.update(1, 0.0, 10.0, 2)
        course, _ = smoother.update(1, 0.0, 10.0, 2)
        self.assertAlmostEqual(course, 0.0)

    def test_unknown_course_counts_as_stationary(self):
        self.assertEqual(target_velocity(None, 5.0), (0.0, 0.0))
        self.assertEqual(target_velocity(90.0, None), (0.0, 0.0))
        self.assertEqual(target_velocity(90.0, 5.0), (90.0, 5.0))


class StationaryTests(unittest.TestCase):
    def test_slow_or_moored_vessels_are_stationary(self):
        self.assertEqual(target_velocity(123.0, 0.3), (0.0, 0.0))
        self.assertEqual(target_velocity(123.0, 0.8, status=5), (0.0, 0.0))  # moored
        self.assertEqual(target_velocity(123.0, 0.8, status=1), (0.0, 0.0))  # anchor
        self.assertEqual(target_velocity(123.0, 0.8, status=0), (123.0, 0.8))
        self.assertEqual(target_velocity(123.0, 3.0, status=5), (123.0, 3.0))  # stale

    def test_own_vessel_at_rest_is_stationary(self):
        ship = SimpleNamespace(get_cog=lambda: 271.0, get_sog=lambda: 0.2)
        self.assertEqual(own_velocity(ship), (0.0, 0.0))

    def test_moored_neighbours_jitter_without_warnings(self):
        import random
        random.seed(2)
        own = SimpleNamespace(get_mmsi=lambda: "1", get_latitude=lambda: 53.26,
                              get_longitude=lambda: 7.39,
                              get_cog=lambda: random.uniform(0, 360),
                              get_sog=lambda: random.choice([0.0, 0.1, 0.2, 0.3]))
        reports = [SimpleNamespace(
            msg_type=18, mmsi=10 + i % 5, lat=53.26 + (20 + 10 * (i % 5)) / 1852 / 60,
            lon=7.39, course=random.uniform(0, 359), speed=random.choice([0.0, 0.2, 0.3]),
            heading=None) for i in range(200)]
        for mode in ("open", "river"):
            que = queue.Queue()
            with patch.object(settings, "NAVIGATION_MODE", mode):
                track_messages(reports, que, own)
            self.assertEqual(que.qsize(), 0, mode)


class ClearanceTests(unittest.TestCase):
    def test_clearance_subtracts_wider_side_and_own_half_beam(self):
        track = CPATrack(mmsi=1, cpa=50 / 1852, to_port=4, to_starboard=6)
        self.assertAlmostEqual(hull_clearance(track) * 1852, 50 - 6 - 1.75)

    def test_unknown_dimensions_use_default_half_beam(self):
        track = CPATrack(mmsi=1, cpa=50 / 1852)
        self.assertAlmostEqual(hull_clearance(track) * 1852, 50 - 3.0 - 1.75)
        self.assertEqual(hull_clearance(CPATrack(mmsi=1, cpa=0.0)), 0.0)

    def test_river_limit_applies_to_clearance(self):
        track = CPATrack(mmsi=1, cpa=55 / 1852, tcpa=1.0, distance=0.2, speed=8.0,
                         msg_type=1, to_port=4, to_starboard=6)
        with patch.object(settings, "NAVIGATION_MODE", "river"):
            self.assertTrue(dangerous_track(track))       # 47 m clearance
            track.to_starboard = track.to_port = 1
            self.assertFalse(dangerous_track(track))      # 52 m clearance

    def test_queued_warning_carries_clearance_and_mode(self):
        que = queue.Queue()
        queue_warning(que, CPATrack(mmsi=1, cpa=50 / 1852), profile=HARBOUR_PROFILE)
        queued = que.get_nowait()
        self.assertEqual(queued.mode, "harbour")
        self.assertAlmostEqual(queued.clearance * 1852, 45.25)


def own_at(sog, cog=0.0):
    return SimpleNamespace(get_mmsi=lambda: "1", get_latitude=lambda: 53.26,
                           get_longitude=lambda: 7.39, get_cog=lambda: cog,
                           get_sog=lambda: sog)


def ahead(meters, course=180.0, speed=0.0, mmsi=7, msg_type=1, status=None):
    return SimpleNamespace(msg_type=msg_type, mmsi=mmsi, lat=53.26 + meters / 1852 / 60,
                           lon=7.39, course=course, speed=speed, heading=None,
                           status=status)


class ManoeuvringTests(unittest.TestCase):
    def queued(self, own, reports, **settings_patch):
        que = queue.Queue()
        with patch.multiple(settings, **{"QUIET_MANOEUVRING": True,
                                         "NAVIGATION_MODE": "open", **settings_patch}):
            track_messages(reports, que, own)
        return [que.get_nowait() for _ in range(que.qsize())]

    def test_detector_hysteresis(self):
        detector = ManoeuvreDetector()
        self.assertTrue(detector.update(2.9))
        self.assertTrue(detector.update(3.4))
        self.assertFalse(detector.update(3.5))
        self.assertFalse(detector.update(3.0))
        self.assertTrue(detector.update(2.0))

    def test_moored_boats_ahead_are_ignored_while_mooring(self):
        # own vessel approaching its berth at 2 kn, moored boats dead ahead
        reports = [ahead(60 - 3 * i, mmsi=10 + i % 3, speed=0.2, status=5)
                   for i in range(12)]
        for mode in ("open", "river"):
            self.assertEqual(self.queued(own_at(2.0), reports,
                                         NAVIGATION_MODE=mode), [], mode)

    def test_same_boats_warned_at_cruising_speed(self):
        reports = [ahead(60 - 3 * i, speed=0.0) for i in range(3)]
        warned = self.queued(own_at(6.0), reports)
        self.assertTrue(warned)
        self.assertEqual({w.mode for w in warned}, {"open"})

    def test_slow_manoeuvring_vessel_gets_harbour_notice(self):
        reports = [ahead(90 - 5 * i, speed=2.0) for i in range(3)]
        warned = self.queued(own_at(1.5), reports)
        self.assertEqual({w.mode for w in warned}, {"harbour"})

    def test_vessel_at_three_knots_or_more_keeps_full_warnings(self):
        reports = [ahead(900 - 30 * i, speed=8.0) for i in range(3)]
        warned = self.queued(own_at(0.0), reports, NAVIGATION_MODE="river")
        self.assertEqual({w.mode for w in warned}, {"river"})

    def test_quiet_manoeuvring_can_be_switched_off(self):
        reports = [ahead(60 - 3 * i, speed=0.0) for i in range(3)]
        warned = self.queued(own_at(2.0), reports, QUIET_MANOEUVRING=False)
        self.assertEqual({w.mode for w in warned}, {"open"})

    def test_harbour_profile_ignores_slow_targets(self):
        track = CPATrack(mmsi=1, cpa=0.0, tcpa=1.0, distance=0.05, speed=0.8, msg_type=1)
        self.assertFalse(dangerous_track(track, HARBOUR_PROFILE))
        track.speed = 2.0
        self.assertTrue(dangerous_track(track, HARBOUR_PROFILE))


class ModeArgumentTests(unittest.TestCase):
    def mode_after(self, *argv):
        import argparse
        parser = argparse.ArgumentParser()
        add_mode_argument(parser)
        with patch.object(settings, "NAVIGATION_MODE", "river"):
            apply_mode_argument(parser.parse_args(list(argv)))
            return settings.NAVIGATION_MODE

    def test_river_is_default_and_open_overrides(self):
        self.assertEqual(self.mode_after(), "river")
        self.assertEqual(self.mode_after("--open"), "open")
        self.assertEqual(self.mode_after("--river"), "river")


if __name__ == "__main__":
    unittest.main()
