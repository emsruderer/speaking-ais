"""Program-wide runtime settings."""

VERBOSE = False

# SignalK NMEA 0183 TCP server hostname or IP address.
HOST = "192.168.20.90"

# Maximum distance at which AIS position reports are tracked, in nautical miles.
# Set to None to track reports at any distance.
AIS_RANGE_NM = 12.0

# Danger thresholds: "open" for open water and wide estuaries, "river" for rivers
# and narrow channels where vessels normally pass within 100 m (see
# ais_stream.PROFILES). The programs accept --open and --river to override it.
NAVIGATION_MODE = "open"

# While the own vessel makes less than 3 kn (mooring, manoeuvring in harbour):
# ignore stationary vessels, give one close-range notice for slow vessels and
# keep normal warnings for vessels making 3 kn or more.
QUIET_MANOEUVRING = False

# Own vessel: beam and GPS antenna offset from the centreline (starboard positive),
# in meters. Used for the hull-to-hull clearance at CPA.
OWN_BEAM_M = 3.5
OWN_ANTENNA_OFFSET_M = 0.0

# Set the system clock from GPS (RMC) when it is off by more than a few seconds.
# Needs CAP_SYS_TIME, e.g. AmbientCapabilities=CAP_SYS_TIME in the systemd unit.
SET_SYSTEM_CLOCK = True
