"""
Calculate closest point of approach and time to closest point of approach
"""
from math import atan2, cos, degrees, radians, sin, sqrt

import settings

# Demo positions 0.54 NM north and south of the own start position
NORTH_POSITION = (settings.OWN_START_LAT + 0.009, settings.OWN_START_LON)
SOUTH_POSITION = (settings.OWN_START_LAT - 0.009, settings.OWN_START_LON)
R = 3440.065  # Radius of earth in nautical miles

def cpa_tcpa(mmsi1, lat1, lon1, cog1, sog1, mmsi2, lat2, lon2, cog2, sog2):
    """
        Calculate CPA and TCPA between two moving vessels.

        Args:
            lat1, lon1: Own ship position (degrees)
            sog1: Speed Over Ground (knots)
            cog1: Course Over Ground (degrees True)
            lat2, lon2: Target ship position (degrees)
            cog2, sog2: Target course (degrees True) and speed (knots)

        Returns:
            A dictionary containing CPA and current distance in nautical miles,
            TCPA in minutes, bearing in degrees, and signed range-closure speed in knots.
            A positive speed means the range is shrinking; a negative speed means it is growing.
            A negative TCPA means the CPA occurred |TCPA| minutes ago.
            rel_east and rel_north are the target's velocity relative to the own ship
            in nautical miles per minute.
    """
    try:
        cog1 = radians(cog1)
        cog2 = radians(cog2)

        v1 = sog1 / 60  # Own ship speed (nm/min)
        v2 = sog2 / 60  # Target ship speed (nm/min)

        v1x = v1 * sin(cog1)  # East component
        v1y = v1 * cos(cog1)  # North component

        v2x = v2 * sin(cog2)  # East component
        v2y = v2 * cos(cog2)  # North component

        dvx = (v2x - v1x)  # relative speed in x (nm/hr)
        dvy = (v2y - v1y)  # relative speed in y (nm/hr
        dv = sqrt(dvx**2 + dvy**2)  # relative speed in nm/min

        d_lat = (lat1 - lat2) * 60  # in nautical miles
        d_lon = (lon1 - lon2) * 60 * cos(radians((lat1 + lat2) / 2))  # nautical miles
        distance = sqrt(d_lon**2 + d_lat**2)
        bearing = degrees(atan2(-d_lon, -d_lat)) % 360

        if dv < 1e-10:
            return {"cpa": distance, "tcpa": float('inf'), "mmsi": mmsi2, "distance": distance, "bearing": bearing, "approaching_speed": 0.0,
                    "rel_east": dvx, "rel_north": dvy}

        tcpa = (d_lat*dvy+ d_lon*dvx)/dv**2  # in minutes
        cpa = sqrt((d_lon - dvx * tcpa)**2 + (d_lat - dvy * tcpa)**2)
        approaching_speed = (
            (d_lat * dvy + d_lon * dvx) / distance * 60
            if distance > 0.0 else 0.0
        )

        return { "cpa": cpa, "tcpa" : tcpa, "mmsi" : mmsi2, "distance": distance, "bearing": bearing, "approaching_speed": approaching_speed,
                 "rel_east": dvx, "rel_north": dvy}
    except ZeroDivisionError as ex:
        print(ex, mmsi1 )
        return { "cpa": None, "tcpa" : None, "mmsi" : mmsi2, "distance": 0.0, "bearing": 0.0, "approaching_speed" : 0.0,
                 "rel_east": 0.0, "rel_north": 0.0 }

def print_cpa(k,v):
    """function pretty print cpa tcpa"""
    if v['distance']< 3:
        if v["tcpa"] < 0.0:
            print(f"The closest point of boat {k} to {v["mmsi"]} is passed, distance {v["distance"]:.2f} nM, bearing {v["bearing"]:.2f} deg. Approaching speed {v["approaching_speed"]:.2f} kn.")
        else:
            print(f"The closest point of boat {k} to {v["mmsi"]} is : {v["cpa"]:.2f} nM in {v["tcpa"]:.2f} min., distance {v["distance"]:.2f} nM, bearing {v["bearing"]:.2f} deg. Approaching speed {v["approaching_speed"]:.2f} kn.")

def show_cpas(cpas):
    """function pretty print dict cpa tcpa"""
    for k,v in cpas.items():
        print_cpa(k,v)

def calc_speed(speed1, speed2, course1, course2):
    """test function for cpa tcpa"""
    course1 = radians(course1)
    course2 = radians(course2)
    dvx = abs(speed1*sin(course1) - speed2*sin(course2))
    dvy = abs(speed1*cos(course1) - speed2*cos(course2))
    dv = sqrt(dvx**2 + dvy**2)
    return f"Delta Vx {dvx:.2f}, Delta Vy {dvy:.2f} => Delta V {dv:.2f} kn."

if __name__ == "__main__":
    #Test speed calculations and cpa tcpa calculations
    print(calc_speed(5.0, 5.0, 0, 180))
    print("CPA and TCPA calculations test")
    cpas_dict = {} 
    cpas_dict = {
        "211040154": cpa_tcpa( 211040154, SOUTH_POSITION[0], SOUTH_POSITION[1],   0,  5.0,  settings.OWN_MMSI,  NORTH_POSITION[0], NORTH_POSITION[1], 180, 5.0),
        "211040155": cpa_tcpa( 211040155, SOUTH_POSITION[0], SOUTH_POSITION[1],   0,  3.0,  settings.OWN_MMSI,  NORTH_POSITION[0], NORTH_POSITION[1],180, 3.0),
        "211040156": cpa_tcpa( 211040156  , SOUTH_POSITION[0], SOUTH_POSITION[1],  180,  3.0,  settings.OWN_MMSI,  NORTH_POSITION[0], NORTH_POSITION[1],180, 10.0),
        "211040157": cpa_tcpa( 211040157  , SOUTH_POSITION[0], SOUTH_POSITION[1],  180,  0.0,  settings.OWN_MMSI,  NORTH_POSITION[0], NORTH_POSITION[1],358, 0.0),
        "211040158": cpa_tcpa( 211040158  , SOUTH_POSITION[0], SOUTH_POSITION[1],  180,  0.1,  settings.OWN_MMSI,  SOUTH_POSITION[0], SOUTH_POSITION[1],358, 0.0)
    }
    show_cpas(cpas_dict)
