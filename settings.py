"""Program-wide runtime settings."""

HOST = "localhost"
PORT = 10110

VERBOSE = False

# Danger thresholds: "open" for open water and wide estuaries, "river" for rivers
# and narrow channels where vessels normally pass within 100 m (see
# ais_stream.PROFILES). The programs accept --open and --river to override it.
NAVIGATION_MODE = "river"

# While the own vessel makes less than 3 kn (mooring, manoeuvring in harbour):
# ignore stationary vessels, give one close-range notice for slow vessels and
# keep normal warnings for vessels making 3 kn or more.
QUIET_MANOEUVRING = False

# Own vessel identity and start position. The start position (decimal degrees)
# is used until the first GPS fix arrives.
OWN_MMSI = 244030153
OWN_START_LAT = 53.264282
OWN_START_LON = 7.397336

# Own vessel: beam and GPS antenna offset from the centreline (starboard positive),
# in meters. Used for the hull-to-hull clearance at CPA.
OWN_BEAM_M = 3.5
OWN_ANTENNA_OFFSET_M = 0.0

# Dead reckoning while GPS is lost (see dead_reckoning.py). The sensor model uses
# heading, speed through water, heel and the current learned while GPS was present.
# False: shadow mode; the model runs alongside the simple course-and-speed
# extrapolation and its error is logged when GPS returns, but warnings use the
# simple method. Set True once the model has been checked on board.
DEAD_RECKONING_SENSORS = False
# Magnetic variation in degrees, east positive, for magnetic heading sentences
# (HDG, HDM). None: use the variation in the HDG sentence, or 0. A remaining
# constant error is learned as heading offset while GPS is present.
MAGNETIC_VARIATION = None
# Leeway in degrees = LEEWAY_COEFFICIENT * heel (degrees, starboard down positive)
# / speed through water (knots) squared; typically 9-12 for a sailing yacht.
# 0 disables leeway until the coefficient and heel sign are checked on board.
LEEWAY_COEFFICIENT = 0.0

# Set the system clock from GPS (RMC) when it is off by more than a few seconds.
# Needs CAP_SYS_TIME, e.g. AmbientCapabilities=CAP_SYS_TIME in the systemd unit.
SET_SYSTEM_CLOCK = True
