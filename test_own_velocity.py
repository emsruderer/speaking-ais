import unittest

from ais_stream import OwnVelocitySmoother, own_velocity


class FakeShip:
    def __init__(self, cog, sog):
        self.cog, self.sog = cog, sog

    def get_cog(self):
        return self.cog

    def get_sog(self):
        return self.sog


class OwnVelocityTest(unittest.TestCase):
    def test_moored_gps_noise_with_stale_course_is_stationary(self):
        clock = [0.0]
        smoother = OwnVelocitySmoother(clock=lambda: clock[0])
        # Values from a moored ship: speed jitters 0.1-1.5 kn, last real course stays
        speeds = [0.57, 0.35, 0.13, 0.15, 0.42, 0.37, 0.98, 1.5, 1.35, 1.03, 1.17,
                  0.49, 0.19, 0.27, 0.3, 0.09, 0.17, 0.19, 0.36, 0.51, 0.53]
        for speed in speeds:
            clock[0] += 1.0
            result = own_velocity(FakeShip(146.85, speed), smoother)
            self.assertEqual(result, (0.0, 0.0))

    def test_real_speed_is_kept(self):
        smoother = OwnVelocitySmoother(clock=lambda: 0.0)
        course, speed = own_velocity(FakeShip(90.0, 4.0), smoother)
        self.assertAlmostEqual(course, 90.0)
        self.assertAlmostEqual(speed, 4.0)

    def test_without_smoother_unchanged(self):
        self.assertEqual(own_velocity(FakeShip(90.0, 0.3)), (0.0, 0.0))
        self.assertEqual(own_velocity(FakeShip(90.0, 2.0)), (90.0, 2.0))


if __name__ == "__main__":
    unittest.main()
