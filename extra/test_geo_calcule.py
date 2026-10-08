import unittest

from extra import geo_calcule


class GeoCalculationsExampleTests(unittest.TestCase):
    def test_warsaw_to_rome_haversine_and_bearing(self):
        warsaw = (52.2296756, 21.0122287)
        rome = (41.8919300, 12.5113300)

        self.assertAlmostEqual(geo_calcule.haversine(*warsaw, *rome), 710.32, places=2)
        self.assertAlmostEqual(geo_calcule.bearing(*warsaw, *rome), 212.46, places=2)

    def test_washington_to_new_york_distance_and_bearing(self):
        washington = (38.9, -77.0)
        new_york = (40.7, -74.0)

        self.assertAlmostEqual(geo_calcule.distance(*washington, *new_york), 175.57, places=2)
        self.assertAlmostEqual(geo_calcule.bearing(*washington, *new_york), 51.06, places=2)

    def test_paris_to_washington_distance(self):
        paris = (48.8566, 2.3522)
        washington = (38.9, -77.0)

        self.assertAlmostEqual(geo_calcule.distance(*paris, *washington), 3327.65, places=2)

    def test_paris_to_washington_cpa_tcpa(self):
        paris = (48.8566, 2.3522)
        washington = (38.9, -77.0)

        result = geo_calcule.cpa_tcpa(*paris, 90, 20, *washington, 270, 15)

        self.assertAlmostEqual(result["cpa"], 2060.06, places=2)
        self.assertAlmostEqual(result["tcpa"], -74.67, places=2)
        self.assertAlmostEqual(result["distance"], 3327.65, places=2)
        self.assertAlmostEqual(result["bearing"], 291.73, places=2)

    def test_london_to_paris_cpa_tcpa(self):
        london = (53.5074, 7.0)
        paris = (48.8566, 2.3522)

        result = geo_calcule.cpa_tcpa(*london, 180, 15.0, *paris, 0, 5.0)

        self.assertAlmostEqual(result["cpa"], 166.03, places=2)
        self.assertAlmostEqual(result["tcpa"], 14.22, places=2)
        self.assertAlmostEqual(result["distance"], 329.34, places=2)
        self.assertAlmostEqual(result["bearing"], 213.90, places=2)

    def test_paris_to_london_cpa_tcpa(self):
        paris = (48.8566, 2.3522)
        london = (53.5074, 7.0)

        result = geo_calcule.cpa_tcpa(*paris, 180, 15.0, *london, 0, 5.0)

        self.assertAlmostEqual(result["cpa"], 183.68, places=2)
        self.assertAlmostEqual(result["tcpa"], -13.67, places=2)
        self.assertAlmostEqual(result["distance"], 329.34, places=2)
        self.assertAlmostEqual(result["bearing"], 30.27, places=2)


if __name__ == "__main__":
    unittest.main()
