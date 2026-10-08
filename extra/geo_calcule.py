"""
geo calculations module
"""
import math as m

RKM = 6371.0  # Radius of earth in kilometers
Rnm = 3440.065  # Radius of earth in nautical miles

JEMGUM_LAT= 53.2644
JEMGUM_LON = 7.3973
JEMGUM = (JEMGUM_LAT,JEMGUM_LON)

NORTH_BORDER = (53.27338,7.3967)
SOUTH_BORDER = (53.25540,7.3949)


def cpa_tcpa(lat1, lon1, course1, speed1,  lat2, lon2, course2, speed2) :
    """Calculate CPA (nmi) and TCPA (hours) from courses (degrees) and speeds (knots)."""
    course1, course2 = m.radians(course1), m.radians(course2)

    dvx = speed1 * m.sin(course1) - speed2 * m.sin(course2)
    dvy = speed1 * m.cos(course1) - speed2 * m.cos(course2)
    relative_speed_squared = dvx**2 + dvy**2

    distance = haversine(lat1, lon1, lat2, lon2)
    bearing_to_other = bearing(lat2, lon2, lat1, lon1)
    angle = m.radians(bearing_to_other)
    east = distance * m.sin(angle)
    north = distance * m.cos(angle)
    initial_bearing = bearing(lat1, lon1, lat2, lon2)

    if relative_speed_squared == 0:
        return {
            "cpa": distance,
            "tcpa": None,
            "distance": distance,
            "bearing": initial_bearing,
        }

    tcpa = -(east * dvx + north * dvy) / relative_speed_squared
    cpa = abs(east * dvy - north * dvx) / m.sqrt(relative_speed_squared)

    return {
        "cpa": cpa,
        "tcpa": tcpa,
        "distance": distance,
        "bearing": initial_bearing,
    }


# True Bearing = Magnetic Bearing + Magnetic Declination
def bearing(lat1, lon1, lat2, lon2):
    """
    Calculate the bearing between two points on the Earth.

    Parameters:
    lat1, lon1 : float : Latitude and Longitude of point 1 in decimal degrees
    lat2, lon2 : float : Latitude and Longitude of point 2 in decimal degrees

    Returns:
    float : Bearing in degrees from point 1 to point 2
    """
    lat1, lon1, lat2, lon2 = map(m.radians, [lat1, lon1, lat2, lon2])

    dlon = lon2 - lon1
    x = m.sin(dlon) * m.cos(lat2)
    y = m.cos(lat1) * m.sin(lat2) - (m.sin(lat1) * m.cos(lat2) * m.cos(dlon))
    initial_bearing = m.atan2(x, y)

    # Convert from radians to degrees and normalize to 0-360
    initial_bearing = m.degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360) % 360

    return compass_bearing


def haversine(lat1, lon1, lat2, lon2):
    """
    Calculate the great-circle distance between two points
    on the Earth specified in decimal degrees using the Haversine formula.

    Parameters:
    lat1, lon1 : float : Latitude and Longitude of point 1 in decimal degrees
    lat2, lon2 : float : Latitude and Longitude of point 2 in decimal degrees

    Returns:
    float : Distance between the two points in nautical miles
    """
     # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(m.radians, [lat1, lon1, lat2, lon2])

    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = m.sin(dlat/2)**2 + m.cos(lat1) * m.cos(lat2) * m.sin(dlon/2)**2
    c = 2 * m.atan2(m.sqrt(a), m.sqrt(1-a))
    r = Rnm # Radius of Earth in nautical miles
    return r * c

def distance(lat1, lon1, lat2, lon2):
    """
    Calculate the distance between two points on the surface of a sphere
    using the spherical law of cosines.

    Parameters:
    lat1, lon1 : float : Latitude and Longitude of point 1 in decimal degrees
    lat2, lon2 : float : Latitude and Longitude of point 2 in decimal degrees

    Returns:
    float : Distance between the two points in nautical miles
    """
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(m.radians, [lat1, lon1, lat2, lon2])

    dlon = lon2 - lon1
    return m.acos(m.sin(lat1) * m.sin(lat2) + m.cos(lat1) * m.cos(lat2) * m.cos(dlon)) * Rnm

if __name__ == "__main__":
    # Example usage
    lat_1, lon_1 = 52.2296756, 21.0122287  # Warsaw
    lat_2, lon_2 = 41.8919300, 12.5113300  # Rome
    lat_3, lon_3 = 48.8566, 2.3522    # Paris
    lat_4, lon_4 = 53.5074, 7.0   # London

    print(f"Distance: {haversine(lat_1, lon_1, lat_2, lon_2):.2f} nm")
    print(f"Bearing: {bearing(lat_1, lon_1, lat_2, lon_2):.2f} degrees")

    lat_5, lon_5 = 38.9, -77.0 # Washington
    lat_6, lon_6 = 40.7, -74.0 # New York City
    print(f"Distance: {distance(lat_5, lon_5, lat_6, lon_6):.2f} nm")
    print(f"Bearing: {bearing(lat_5, lon_5, lat_6, lon_6):.2f} degrees")

    d = distance(lat_3, lon_3, lat_5, lon_5)
    print(f"{d:.0f} nm")
    print(cpa_tcpa(lat_3, lon_3, 90, 20, lat_5, lon_5, 270, 15))
    print(cpa_tcpa(lat_4, lon_4, 180, 15.0, lat_3, lon_3, 0, 5.0))
    print(cpa_tcpa(lat_3, lon_3, 180, 15.0, lat_4, lon_4, 0, 5.0))
