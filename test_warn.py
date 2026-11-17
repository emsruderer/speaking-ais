"""Tests for AIS warning message generation."""

import dataclasses
import math
import queue
import unittest
from unittest.mock import patch

import warn
from cpa_tracker import CPATrack
from link_monitor import Alert


class WarningTests(unittest.TestCase):
    def setUp(self):
        warn.last_call.clear()

    def test_track_with_unavailable_dynamic_fields_still_warns(self):
        [urgent, details] = self.run_tracks(CPATrack(
            mmsi=244000001, course=None, speed=None, heading=None, ship_type=200,
            cpa=0.4, tcpa=10.0, distance=2.0, bearing=180.0, approaching_speed=4.0,
        ), nationality="Test country ")
        self.assertEqual(urgent, (
            "An approaching vessel. Closest point of approach seven hundred forty "
            "one meters, in ten minutes. Currently two nautical miles away, at an "
            "approach speed of four knots."
        ))
        self.assertEqual(details, (
            "Details: a vessel from Test country, bearing one hundred eighty degrees. "
            "MMSI two four four zero zero zero zero zero one."
        ))

    def test_angle_difference_wraps_around_north(self):
        self.assertEqual(warn.angle_difference(5, 355), 10)
        self.assertEqual(warn.angle_difference(355, 5), 10)
        self.assertEqual(warn.angle_difference(90, 270), 180)

    def test_nautical_mile_conversions(self):
        self.assertEqual(warn.nm_to_meters(0.5), (926.0, True))
        # 1 NM north is 1.852 km
        self.assertAlmostEqual(warn.distance(53.0 + 1 / 60, 7.0, 53.0, 7.0), 1.0, places=2)

    def run_tracks(self, *track_list, nationality="Netherlands "):
        tracks = queue.Queue()
        for track in track_list:
            tracks.put(track)
        tracks.put(None)
        with patch.object(warn, "nationaliteit", return_value=nationality), \
                patch.object(warn.register_msg, "add_msg") as add_msg:
            warn.do_warn(tracks)
        return [call.args[0] for call in add_msg.call_args_list]

    def test_urgent_part_first_then_details(self):
        [urgent, details] = self.run_tracks(CPATrack(
            mmsi=244000001, speed=0.3, course=120.0, heading=200, callsign="PD1234",
            shipname="JOHANNA", ship_type=70, destination="ROTTERDAM",
            cpa=0.4, tcpa=10.0,
            distance=2.0, bearing=180.0, approaching_speed=4.0,
        ))
        self.assertEqual(urgent, (
            "An approaching cargo vessel named JOHANNA. Closest point of approach "
            "seven hundred forty one meters, in ten minutes. Currently two nautical "
            "miles away, at an approach speed of four knots. Call sign Papa Delta "
            "one two three four."
        ))
        self.assertEqual(details, (
            "Details: a Dutch cargo vessel, making zero point three knots, on course "
            "one hundred twenty degrees, with compass heading two hundred degrees, "
            "bearing one hundred eighty degrees. Bound for rotterdam. "
            "MMSI two four four zero zero zero zero zero one."
        ))

    def test_details_wait_while_another_vessel_is_due(self):
        spoken = self.run_tracks(
            CPATrack(mmsi=1, shipname="FIRST", cpa=0.4, tcpa=5.0, distance=2.0),
            CPATrack(mmsi=2, shipname="SECOND", cpa=0.4, tcpa=8.0, distance=2.0),
        )
        self.assertEqual(len(spoken), 3)
        self.assertTrue(spoken[0].startswith("An approaching vessel named FIRST."))
        self.assertTrue(spoken[1].startswith("An approaching vessel named SECOND."))
        self.assertTrue(spoken[2].startswith("Details:"))
        self.assertIn("MMSI two.", spoken[2])

    def test_repeat_reports_of_announced_vessel_do_not_block_details(self):
        tracks = queue.Queue()
        tracks.put(CPATrack(mmsi=1, cpa=0.4, tcpa=5.0, distance=2.0))
        newer = CPATrack(mmsi=1, cpa=0.4, tcpa=4.9, distance=1.9)

        def speak(text, _speech_api=None):
            spoken.append(text)
            if len(spoken) == 1:  # a newer report arrives while speaking
                tracks.put(newer)
                tracks.put(None)

        spoken = []
        with patch.object(warn, "nationaliteit", return_value="Netherlands "), \
                patch.object(warn.register_msg, "add_msg", side_effect=speak):
            warn.do_warn(tracks)
        self.assertEqual(len(spoken), 2)
        self.assertTrue(spoken[1].startswith("Details:"))

    def test_alert_is_spoken_first_and_holds_back_details(self):
        tracks = queue.Queue()
        tracks.put(CPATrack(mmsi=1, shipname="FIRST", cpa=0.4, tcpa=5.0, distance=2.0))
        tracks.put(Alert("gps", "GPS position lost."))
        spoken = []

        def speak(text, _speech_api=None):
            spoken.append(text)
            if len(spoken) == 2:  # an alert arrives during the vessel warning
                tracks.put(Alert("ais", "AIS data lost."))
                tracks.put(None)

        with patch.object(warn, "nationaliteit", return_value="Netherlands "), \
                patch.object(warn.register_msg, "add_msg", side_effect=speak):
            warn.do_warn(tracks)
        self.assertEqual(spoken[:3], ["GPS position lost.",
                                      spoken[1], "AIS data lost."])
        self.assertTrue(spoken[1].startswith("An approaching vessel named FIRST."))
        self.assertEqual(len(spoken), 3)  # details skipped for the waiting alert

    def test_unsupported_callsign_characters_are_skipped(self):
        [urgent, _] = self.run_tracks(CPATrack(
            mmsi=244000001, callsign="PD 1-2", cpa=0.4, tcpa=10.0, distance=2.0,
        ))
        self.assertIn("Call sign Papa Delta one two.", urgent)

    def test_german_warning_uses_german_text_and_units(self):
        class GermanSpeech:
            @staticmethod
            def str_number(number):
                return "eins" if number == 1 else str(number)

            spell_alphabet = {"P": "Paula", "D": "Dora", "1": "eins"}

            @staticmethod
            def spell_callsign(callsign):
                return " ".join(GermanSpeech.spell_alphabet[char] for char in callsign)

        with patch.object(warn, "nationaliteit", return_value="Netherlands "):
            urgent, details = warn.compose_warning(
                CPATrack(
                    mmsi=1, callsign="PD1", shipname="JOHANNA", ship_type=70,
                    speed=6.0, course=120.0, cpa=0.4, tcpa=10.0, distance=2.0,
                    bearing=180.0, approaching_speed=4.0,
                ),
                GermanSpeech,
                language="de",
            )
        self.assertIn(
            "Ein Schiff vom Typ Frachtschiff namens JOHANNA nähert sich", urgent
        )
        self.assertIn("Nächster Annäherungspunkt 741 Meter, in 10 Minuten", urgent)
        self.assertIn("Seemeilen entfernt", urgent)
        self.assertIn("Rufzeichen Paula Dora eins", urgent)
        self.assertIn("Frachtschiff aus den Niederlanden", details)
        self.assertIn("Kurs 120 Grad", details)
        self.assertIn("MMSI eins", details)

    def test_german_alert_translation(self):
        self.assertEqual(
            warn.localize_alert(Alert("gps", "GPS position restored."), "de"),
            "GPS-Position wiederhergestellt.",
        )

    def test_distances_at_or_above_one_nautical_mile_keep_nautical_units(self):
        [urgent, _] = self.run_tracks(CPATrack(
            mmsi=244000001, cpa=1.25, tcpa=10.0, distance=2.75,
        ))
        self.assertIn("approach one point two five nautical miles", urgent)
        self.assertIn("two point seven five nautical miles away", urgent)
        self.assertNotRegex(urgent, r"(?<!nautical) miles\b")

    def test_failing_track_does_not_stop_later_warnings(self):
        good = CPATrack(mmsi=244000001, cpa=0.4, tcpa=10.0, distance=2.0)
        with patch.object(warn, "shiptype", side_effect=[RuntimeError("boom"), "Tug"]), \
                self.assertLogs("warn", "ERROR"):
            messages = self.run_tracks(
                CPATrack(mmsi=1, ship_type=52, cpa=0.4, tcpa=10.0, distance=2.0),
                dataclasses.replace(good, ship_type=52),
            )
        self.assertEqual(len(messages), 2)
        self.assertIn("tug vessel", messages[0])


class FastAndCloseTests(unittest.TestCase):
    def setUp(self):
        warn.last_call.clear()

    def speak(self, track):
        tracks = queue.Queue()
        tracks.put(track)
        tracks.put(None)
        with patch.object(warn, "nationaliteit", return_value="Netherlands "), \
                patch.object(warn.register_msg, "add_msg") as add_msg:
            warn.do_warn(tracks)
        return [call.args[0] for call in add_msg.call_args_list]

    def test_fast_vessel_gives_speed_and_mmsi_without_call_sign(self):
        [urgent, _] = self.speak(CPATrack(
            mmsi=211000001, ship_type=60, speed=35.0, cpa=0.2, tcpa=7.0, distance=4.9))
        self.assertTrue(urgent.startswith(
            "An approaching fast passenger vessel, at thirty five knots."))
        self.assertTrue(urgent.endswith("MMSI two one one zero zero zero zero zero one."))

    def test_high_speed_craft_is_not_called_fast_twice(self):
        [urgent, _] = self.speak(CPATrack(
            mmsi=1, ship_type=40, speed=30.0, callsign="DA1", cpa=0.2, tcpa=7.0))
        self.assertTrue(urgent.startswith(
            "An approaching high-speed craft, at thirty knots."))
        self.assertTrue(urgent.endswith("Call sign Delta Alpha one."))

    def test_close_range_short_danger_form_in_seconds(self):
        spoken = self.speak(CPATrack(
            mmsi=1, shipname="SWIFT", speed=40.0, cpa=0.05, tcpa=0.6, distance=0.4,
            bearing=45.0, callsign="DB2"))
        self.assertEqual(spoken, [
            "Danger! Fast vessel named SWIFT close, at forty knots, bearing forty five "
            "degrees. Closest approach ninety three meters in thirty five seconds. "
            "Call sign Delta Bravo two."])

    def test_tcpa_below_two_minutes_in_seconds(self):
        [urgent, _] = self.speak(CPATrack(mmsi=1, cpa=0.2, tcpa=1.5, distance=0.5))
        self.assertIn("in ninety seconds", urgent)


class AdvanceTrackTests(unittest.TestCase):
    def test_head_on_target_moves_closer(self):
        # 2 NM north, closing at 0.6 NM/min (36 kn), 30 s old
        track = CPATrack(mmsi=1, cpa=0.0, tcpa=2 / 0.6, distance=2.0, bearing=0.0,
                         rel_east=0.0, rel_north=-0.6, cpa_time=100.0)
        moved = warn.advance_track(track, at_time=130.0)
        self.assertAlmostEqual(moved.distance, 1.7)
        self.assertAlmostEqual(moved.tcpa, 2 / 0.6 - 0.5)
        self.assertAlmostEqual(moved.bearing, 0.0)
        self.assertAlmostEqual(moved.approaching_speed, 36.0)
        self.assertEqual(track.distance, 2.0)  # original untouched

    def test_crossing_target_bearing_changes(self):
        track = CPATrack(mmsi=1, cpa=0.5, tcpa=1.0, distance=math.hypot(0.5, 1.0),
                         bearing=math.degrees(math.atan2(1.0, 0.5)),
                         rel_east=-1.0, rel_north=0.0, cpa_time=0.0)
        moved = warn.advance_track(track, at_time=60.0)
        self.assertAlmostEqual(moved.distance, 0.5)
        self.assertAlmostEqual(moved.bearing, 0.0)
        self.assertAlmostEqual(moved.tcpa, 0.0)

    def test_passed_cpa_is_not_spoken(self):
        track = CPATrack(mmsi=1, cpa=0.1, tcpa=0.2, distance=0.2, bearing=0.0,
                         rel_east=0.0, rel_north=-1.0, cpa_time=0.0)
        with patch("warn.time.monotonic", return_value=30.0), \
                patch.object(warn.register_msg, "add_msg") as add_msg:
            self.assertIsNone(warn.warn_track(track, None))
        add_msg.assert_not_called()




class ModeWordingTests(unittest.TestCase):
    def setUp(self):
        warn.last_call.clear()
        warn.harbour_notices.clear()

    def speak(self, *tracks):
        que = queue.Queue()
        for track in tracks:
            que.put(track)
        que.put(None)
        with patch.object(warn, "nationaliteit", return_value="Netherlands "), \
                patch.object(warn.register_msg, "add_msg") as add_msg:
            warn.do_warn(que)
        return [call.args[0] for call in add_msg.call_args_list]

    def test_river_speaks_clearance_approximately(self):
        [urgent, _] = self.speak(CPATrack(
            mmsi=1, cpa=0.03, clearance=43 / 1852, tcpa=3.0, distance=0.3, mode="river"))
        self.assertIn("Closest point of approach about forty meters, in three minutes",
                      urgent)
        self.assertIn("about five hundred sixty meters away", urgent)

    def test_open_mode_speaks_exact_clearance(self):
        [urgent, _] = self.speak(CPATrack(
            mmsi=1, cpa=0.03, clearance=0.02, tcpa=3.0, distance=0.3, mode="open"))
        self.assertIn("Closest point of approach thirty seven meters", urgent)

    def test_harbour_notice_once_per_vessel(self):
        track = CPATrack(mmsi=244000001, shipname="TUG", ship_type=52, cpa=0.003,
                         tcpa=1.5, distance=0.04, bearing=90.0, callsign="DC3",
                         speed=2.0, mode="harbour")
        spoken = self.speak(track, dataclasses.replace(track, mmsi=5))
        self.assertEqual(spoken[0], (
            "Tug vessel named TUG manoeuvring close, bearing ninety degrees, about "
            "seventy meters away. Call sign Delta Charlie three."))
        self.assertEqual(len(spoken), 2)
        self.assertEqual(self.speak(track), [])  # same vessel again: no repeat




class RepeatCallTests(unittest.TestCase):
    def setUp(self):
        warn.last_call.clear()

    def test_rewarns_when_tcpa_drops_sharply(self):
        clock = [1000.0]
        with patch("warn.time.monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(warn.repeat_call(1, 29.0))
            clock[0] += 30
            self.assertFalse(warn.repeat_call(1, 20.0))  # predicted 28.5 min
            clock[0] += 30
            self.assertTrue(warn.repeat_call(1, 5.0))    # predicted 28 min
            clock[0] += 30
            self.assertFalse(warn.repeat_call(1, 4.5))

    def test_steady_approach_waits_half_the_tcpa(self):
        clock = [0.0]
        with patch("warn.time.monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(warn.repeat_call(1, 20.0))
            clock[0] += 9.9 * 60
            self.assertFalse(warn.repeat_call(1, 10.1))
            clock[0] += 60
            self.assertTrue(warn.repeat_call(1, 10.0))


class CollectTracksTests(unittest.TestCase):
    def test_keeps_newest_per_vessel(self):
        que = queue.Queue()
        for track in (CPATrack(mmsi=1, tcpa=20.0), CPATrack(mmsi=2, tcpa=8.0),
                      CPATrack(mmsi=1, tcpa=5.0)):
            que.put(track)
        pending = {}
        self.assertFalse(warn.collect_tracks(que, pending))
        self.assertEqual(sorted((t.mmsi, t.tcpa) for t in pending.values()),
                         [(1, 5.0), (2, 8.0)])

    def test_sentinel_reports_shutdown_after_pending_tracks(self):
        que = queue.Queue()
        que.put(CPATrack(mmsi=1, tcpa=5.0))
        que.put(None)
        pending = {}
        self.assertTrue(warn.collect_tracks(que, pending))
        self.assertEqual(list(pending), [1])


if __name__ == "__main__":
    unittest.main()
