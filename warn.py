"""
Talking AIS watcher
"""
import dataclasses
import math
import time
from logging import getLogger
from queue import Empty

import register_msg
from cpa_tracker import CPATrack
from link_monitor import Alert
from nationality import get_country
from pyais.filter import haversine
from shipstype import shiptype
import settings
from queue_metrics import increment

HOST = settings.HOST
PORT = settings.PORT
TALKING = True

NATIONALITY_ADJECTIVES = {
    "Belgium": "Belgian",
    "Denmark": "Danish",
    "Finland": "Finnish",
    "France": "French",
    "Germany": "German",
    "Netherlands": "Dutch",
    "Norway": "Norwegian",
    "Sweden": "Swedish",
    "United Kingdom": "British",
}

log = getLogger(__name__)
log.setLevel("INFO")

FIELDS = dataclasses.fields(CPATrack)
last_call = {}  # mmsi -> (time.monotonic(), tcpa) of the last spoken warning
FORGET_CALL_SECONDS = 3600
URGENCY_FACTOR = 0.5  # re-warn at once when tcpa is below half the predicted tcpa
MINIMUM_CALL_INTERVAL = 15  # seconds; keeps close-range warnings from overlapping
FAST_SPEED = 20  # knots; faster vessels are announced as fast
HIGH_SPEED_CRAFT_TYPES = range(40, 50)
CLOSE_TCPA = 1  # minutes; below this the short danger form is spoken
APPROXIMATE_MODES = ("river", "harbour")  # distances spoken as 'about', per 10 m
HARBOUR_NOTICE_SECONDS = 600  # a harbour notice per vessel at most this often
harbour_notices = {}  # mmsi -> time.monotonic() of the last harbour notice
SECONDS_TCPA = 2  # minutes; below this tcpa is spoken in seconds


def call_interval(tcpa):
    """Seconds to wait before repeating a warning: half the time to CPA."""
    return max(MINIMUM_CALL_INTERVAL, (tcpa * 60)/2)

def nationaliteit(mmsi):
    """
    nationaliteit based on mmsi
    """
    return get_country(mmsi)[1] + " "

def bearing(lat1,lon1,lat2,lon2):
    """Calculate bearing between two lat/lon points in degrees"""
    if lat1 is not None and lon1 is not None:
        lat1,lon1 = math.radians(lat1), math.radians(lon1)
        lat2,lon2 = math.radians(lat2), math.radians(lon2)

        d_lon = lon2 - lon1

        y = math.sin(d_lon) * math.cos(lat2)
        x = math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(d_lon)

        direction_rad = math.atan2(y,x)
        direction_deg = math.degrees(direction_rad)

        return int((direction_deg+360)%360)
    else:
        return 0

def distance(lat,lon,lat2,lon2):
    """Calculate distance between two lat/lon points in nautical miles"""
    if lat is not None and lon is not None:
        return haversine((lat2,lon2), (lat,lon)) / 1.852  # haversine is in km
    return 0.0

def angle_difference(a, b):
    """Smallest difference between two compass directions in degrees (0-180)."""
    return abs((a - b + 180) % 360 - 180)

def is_due(mmsi, tcpa):
    """Return whether a warning for mmsi with this tcpa should be spoken now.

    A warning is repeated after half the tcpa of the previous call, or at once
    when the vessel became much more urgent than predicted (it or the own ship
    turned or sped up), so a sudden drop in tcpa is never left unannounced."""
    if mmsi not in last_call:
        return True
    called, called_tcpa = last_call[mmsi]
    elapsed = time.monotonic() - called
    predicted_tcpa = called_tcpa - elapsed / 60
    return (elapsed >= call_interval(called_tcpa)
            or tcpa < predicted_tcpa * URGENCY_FACTOR)

def repeat_call(mmsi, tcpa):
    """Determine if we should repeat the call for this mmsi based on tcpa,
    recording the call when it is due (see is_due)."""
    current_time = time.monotonic()
    for old_mmsi, (called, _) in list(last_call.items()):
        if current_time - called > FORGET_CALL_SECONDS:
            del last_call[old_mmsi]
    if not is_due(mmsi, tcpa):
        return False
    last_call[mmsi] = (current_time, tcpa)
    return True

def nm_to_meters(nm):
    """Convert nautical miles to meters if less than 1 nM, otherwise no change."""
    if 0.0 <= nm < 1.0 :
        return nm * 1852, True
    return nm, False

def min_to_hr(minutes):
    """Convert minutes to hours."""
    if minutes > 120.0:
        return minutes / 60, True
    return minutes, False

def spoken_callsign(callsign, speech_api):
    """Spell a call sign, skipping characters the phonetic alphabet does not cover."""
    letters = "".join(c for c in callsign.upper() if c in speech_api.spell_alphabet)
    return speech_api.spell_callsign(letters) if letters else None

def spoken_mmsi(mmsi, speech_api):
    """Speak an MMSI one digit at a time."""
    return " ".join(speech_api.str_number(int(digit)) for digit in str(mmsi))

def collect_tracks(que, pending, activity_counter=None, block=True):
    """Move queued tracks into pending (mmsi -> newest track); return True on shutdown.

    Speech is slow, so the queue holds a backlog of older snapshots by the time a
    warning finishes; only the latest snapshot of each vessel is worth speaking.
    With block, waits for at least one queue entry."""
    try:
        msg = que.get() if block else que.get_nowait()
        while True:
            if msg is None:
                return True
            increment(activity_counter)
            pending.pop(pending_key(msg), None)
            pending[pending_key(msg)] = msg
            msg = que.get_nowait()
    except Empty:
        return False

def pending_key(msg):
    """One pending entry per vessel, and one per alert kind."""
    if isinstance(msg, Alert):
        return ("alert", msg.kind)
    return getattr(msg, "mmsi", None)

def urgency(track):
    """Sort key: system alerts first, then lowest tcpa."""
    if isinstance(track, Alert):
        return -math.inf
    tcpa = getattr(track, "tcpa", None)
    return math.inf if tcpa is None else tcpa

def waiting(msg):
    """Whether a pending entry will be spoken (so details must wait for it)."""
    if isinstance(msg, Alert):
        return True
    if getattr(msg, "mode", None) == "harbour":
        called = harbour_notices.get(msg.mmsi)
        return called is None or time.monotonic() - called >= HARBOUR_NOTICE_SECONDS
    return is_due(msg.mmsi, msg.tcpa)

def localize_alert(alert, language):
    """Return a system alert in the selected announcement language."""
    if language != "de":
        return alert.text
    restored = "restored" in alert.text.lower()
    alerts = {
        ("gps", False): "Warnung: GPS-Position verloren. Kollisionswarnungen "
                        "verwenden die Koppelnavigation.",
        ("gps", True): "GPS-Position wiederhergestellt.",
        ("ais", False): "Warnung: AIS-Daten verloren. Es werden keine "
                        "Schiffsmeldungen empfangen.",
        ("ais", True): "AIS-Daten wiederhergestellt.",
        ("link", False): "Warnung: GPS- und AIS-Daten verloren. Bitte die "
                         "SignalK-Verbindung prüfen.",
    }
    return alerts.get((alert.kind, restored), alert.text)

def do_warn(que, activity_counter=None, language="en"):
    """Load speech in the warning worker and process AIS tracks.

    GPS/AIS alerts are spoken first, then the most urgent vessel. A vessel's
    details follow only when no alert or other vessel warning is waiting."""
    if language == "de":
        import sprich as speech
    elif language == "en":
        import speech
    else:
        raise ValueError(f"Unsupported speech language: {language!r}")

    pending = {}
    stop = False
    while True:
        if not stop:
            stop = collect_tracks(que, pending, activity_counter, block=not pending)
        if not pending:
            return
        msg = min(pending.values(), key=urgency)
        del pending[pending_key(msg)]
        try:
            if isinstance(msg, Alert):
                register_msg.add_msg(localize_alert(msg, language), speech)
                continue
            details = warn_track(msg, speech, language)
            if details is None:
                continue
            if not stop:
                stop = collect_tracks(que, pending, activity_counter, block=False)
            if not any(waiting(item) for item in pending.values()):
                register_msg.add_msg(details, speech)
        except Exception:  # one bad track must not stop all later warnings
            log.exception("Could not issue warning for %s", getattr(msg, "mmsi", msg))

def vessel_description(ship_type, language="en"):
    """Spoken vessel type such as 'cargo vessel', or None when the type is unknown."""
    if not ship_type:
        return None
    vessel = shiptype(ship_type).lower()
    if vessel.startswith(("unknown", "reserved", "not available")) or "reserved" in vessel:
        return None
    if language == "de":
        descriptions = {
            "wing in ground": "Bodeneffektfahrzeug",
            "fishing": "Fischereifahrzeug",
            "towing": "Schleppfahrzeug",
            "dredging or underwater operations": "Bagger- oder Unterwasserfahrzeug",
            "diving operations": "Tauchfahrzeug",
            "military": "Militärfahrzeug",
            "sailing": "Segelschiff",
            "pleasure craft": "Sportboot",
            "high-speed craft": "Hochgeschwindigkeitsfahrzeug",
            "pilot vessel": "Lotsenboot",
            "search and rescue vessel": "Seenotrettungsschiff",
            "tug": "Schlepper",
            "port tender": "Hafenfahrzeug",
            "anti-pollution vessel": "Ölbekämpfungsschiff",
            "law enforcement vessel": "Polizeischiff",
            "medical transport": "medizinisches Transportfahrzeug",
            "non-combatant ship": "unbewaffnetes Schiff",
            "passenger": "Passagierschiff",
            "cargo": "Frachtschiff",
            "tanker": "Tanker",
            "other": "sonstiges Schiff",
        }
        base, separator, hazard = vessel.partition(", hazardous category ")
        translated = descriptions.get(base, "sonstiges Schiff")
        if separator:
            translated += f", Gefahrgutkategorie {hazard.upper()}"
        return translated
    return vessel if vessel.endswith(("vessel", "craft")) else vessel + " vessel"

def distance_words(nm, speech_api, approximate=False, language="en"):
    """Speak a distance in meters below one nautical mile, else in nautical miles.

    approximate rounds meters to tens and says 'about': AIS positions can be
    9 m off for each vessel."""
    d, in_meters = nm_to_meters(nm)
    if language == "de":
        if in_meters and approximate:
            return "ungefähr " + speech_api.str_number(max(10, 10 * round(d / 10))) + " Meter"
        if in_meters:
            return speech_api.str_number(round(d)) + " Meter"
        return speech_api.str_number(round(d, 2)) + " Seemeilen"
    if in_meters and approximate:
        return "about " + speech_api.str_number(max(10, 10 * round(d / 10))) + " meters"
    if in_meters:
        return speech_api.str_number(round(d)) + " meters"
    return speech_api.str_number(round(d, 2)) + " nautical miles"

def advance_track(msg, at_time=None):
    """Return a copy of msg with CPA data moved forward to at_time (monotonic seconds).

    The snapshot is tens of seconds old when spoken (queue, speech synthesis); at a
    closing speed of 50 kn that is several hundred meters. Assumes both vessels keep
    course and speed, as the CPA calculation itself does."""
    if (msg.cpa_time is None or msg.rel_east is None or msg.rel_north is None
            or msg.distance is None or msg.bearing is None or msg.tcpa is None):
        return msg
    if at_time is None:
        at_time = time.monotonic()
    minutes = max(0.0, at_time - msg.cpa_time) / 60
    bearing_rad = math.radians(msg.bearing)
    east = msg.distance * math.sin(bearing_rad) + msg.rel_east * minutes
    north = msg.distance * math.cos(bearing_rad) + msg.rel_north * minutes
    distance = math.hypot(east, north)
    approaching = (-(east * msg.rel_east + north * msg.rel_north) / distance * 60
                   if distance > 0 else 0.0)
    return dataclasses.replace(
        msg, tcpa=msg.tcpa - minutes, distance=distance,
        bearing=math.degrees(math.atan2(east, north)) % 360,
        approaching_speed=approaching, cpa_time=at_time,
    )

def is_fast(msg):
    """Whether a vessel is a high-speed craft or sails faster than FAST_SPEED."""
    return (msg.ship_type in HIGH_SPEED_CRAFT_TYPES
            or (msg.speed is not None and msg.speed > FAST_SPEED))

def tcpa_words(tcpa, speech_api, language="en"):
    """Speak a tcpa in seconds below two minutes, in hours above two hours."""
    if tcpa < SECONDS_TCPA:
        seconds = max(5, 5 * round(tcpa * 60 / 5))
        unit = "Sekunden" if language == "de" else "seconds"
        return speech_api.str_number(seconds) + " " + unit
    t, in_hours = min_to_hr(tcpa)
    if language == "de":
        return speech_api.str_number(round(t)) + (" Stunden" if in_hours else " Minuten")
    return speech_api.str_number(round(t)) + (" hours" if in_hours else " minutes")

def identification(msg, speech_api, language="en"):
    """Spoken call sign, or the MMSI when the vessel sent no usable call sign."""
    spelled = spoken_callsign(msg.callsign, speech_api) if msg.callsign else None
    if spelled:
        return ("Rufzeichen " if language == "de" else "Call sign ") + spelled
    return "MMSI " + spoken_mmsi(msg.mmsi, speech_api)

def compose_warning(msg, speech_api, language="en"):
    """Return (urgent, details) texts for a dangerous track.

    urgent:  ship type and name, CPA, TCPA, distance, approach speed and call sign.
             Fast vessels add their speed; for fast vessels and close range the
             call sign (or MMSI) is always given. Within CLOSE_TCPA a short danger
             form with bearing is used and details is None.
    details: nationality, speed, course, heading, bearing, destination and MMSI."""
    approximate = msg.mode in APPROXIMATE_MODES
    cpa = msg.clearance if msg.clearance is not None else msg.cpa
    german = language == "de"
    vessel_noun = vessel_description(msg.ship_type, language) or (
        "Schiff" if german else "vessel"
    )
    vessel = vessel_noun if vessel_noun != "vessel" else None
    fast = is_fast(msg)
    if german:
        name = "Ein schnell fahrendes Schiff" if fast else "Ein Schiff"
        if vessel_noun != "Schiff":
            name += " vom Typ " + vessel_noun
        if msg.shipname:
            name += " namens " + msg.shipname
    else:
        if fast and not vessel_noun.startswith("high-speed"):
            vessel = "fast " + vessel_noun
        name = (vessel or "vessel") + (
            " named " + msg.shipname if msg.shipname else ""
        )
    speed = (
        (", mit " if german else ", at ")
        + speech_api.str_number(round(msg.speed))
        + (" Knoten" if german else " knots")
        if fast and msg.speed is not None else ""
    )
    tcpa_known = msg.tcpa is not None and math.isfinite(msg.tcpa)

    if tcpa_known and msg.tcpa < CLOSE_TCPA:
        if german:
            urgent = "Gefahr! " + name + " befindet sich in unmittelbarer Nähe" + speed
        else:
            urgent = "Danger! " + name[0].upper() + name[1:] + " close" + speed
        if msg.bearing is not None:
            urgent += (
                ", Peilung " if german else ", bearing "
            ) + speech_api.str_number(round(msg.bearing)) + (
                " Grad" if german else " degrees"
            )
        urgent += "."
        if cpa is not None:
            urgent += (
                (" Nächster Annäherungspunkt " if german else " Closest approach ")
                + distance_words(cpa, speech_api, approximate, language)
                + (" in " if german else " in ")
                + tcpa_words(msg.tcpa, speech_api, language) + "."
            )
        return urgent + " " + identification(msg, speech_api, language) + ".", None

    urgent = name + " nähert sich" + speed + "." if german else (
        "An approaching " + name + speed + "."
    )
    if cpa is not None or tcpa_known:
        urgent += " Nächster Annäherungspunkt" if german else " Closest point of approach"
        if cpa is not None:
            urgent += " " + distance_words(cpa, speech_api, approximate, language)
        if tcpa_known:
            urgent += (", in " if cpa is not None else " in ") + tcpa_words(
                msg.tcpa, speech_api, language)
        urgent += "."
    current = []
    if msg.distance is not None:
        current.append(
            distance_words(msg.distance, speech_api, approximate, language)
            + (" entfernt" if german else " away")
        )
    if msg.approaching_speed is not None:
        current.append(
            ("Annäherungsgeschwindigkeit " if german else "at an approach speed of ")
            + speech_api.str_number(round(msg.approaching_speed))
            + (" Knoten" if german else " knots")
        )
    if current:
        urgent += (" Derzeit " if german else " Currently ") + ", ".join(current) + "."
    if fast:
        urgent += " " + identification(msg, speech_api, language) + "."
    else:
        spelled_callsign = (spoken_callsign(msg.callsign, speech_api)
                            if msg.callsign else None)
        if spelled_callsign:
            urgent += (" Rufzeichen " if german else " Call sign ") + spelled_callsign + "."

    nationality = nationaliteit(msg.mmsi).strip()
    german_countries = {
        "Belgium": "Belgien",
        "Denmark": "Dänemark",
        "Finland": "Finnland",
        "France": "Frankreich",
        "Germany": "Deutschland",
        "Netherlands": "den Niederlanden",
        "Norway": "Norwegen",
        "Sweden": "Schweden",
        "United Kingdom": "dem Vereinigten Königreich",
    }
    if german:
        country = german_countries.get(nationality, nationality)
        flag = (
            (vessel_noun + " aus " + country)
            if nationality and nationality != "Unknown" else None
        )
    elif nationality in NATIONALITY_ADJECTIVES:
        flag = "a " + NATIONALITY_ADJECTIVES[nationality] + " " + vessel_noun
    elif nationality and nationality != "Unknown":
        flag = "a " + vessel_noun + " from " + nationality
    else:
        flag = None
    facts = [flag] if flag else []
    if msg.speed is not None:
        facts.append(
            ("Geschwindigkeit " if german else "making ")
            + speech_api.str_number(msg.speed)
            + (" Knoten" if german else " knots")
        )
    if msg.course is not None:
        facts.append(
            ("Kurs " if german else "on course ")
            + speech_api.str_number(int(msg.course))
            + (" Grad" if german else " degrees")
        )
        if (msg.heading is not None and msg.heading != 511
                and angle_difference(msg.heading, msg.course) >= 10):
            facts.append(
                ("Kompasskurs " if german else "with compass heading ")
                + speech_api.str_number(round(msg.heading))
                + (" Grad" if german else " degrees")
            )
    if msg.bearing is not None:
        facts.append(
            ("Peilung " if german else "bearing ")
            + speech_api.str_number(round(msg.bearing))
            + (" Grad" if german else " degrees")
        )
    details = "Details: " + ", ".join(facts) + "." if facts else ""
    if msg.destination:
        details += (
            (" Zielhafen " if german else " Bound for ")
            + msg.destination.lower() + "."
        )
    details += " MMSI " + spoken_mmsi(msg.mmsi, speech_api) + "."
    return urgent, details.strip()

def compose_harbour_notice(msg, speech_api, language="en"):
    """Short notice for a vessel manoeuvring close to the moored own vessel."""
    german = language == "de"
    vessel = vessel_description(msg.ship_type, language) or (
        "Schiff" if german else "vessel"
    )
    name = vessel[0].upper() + vessel[1:]
    if msg.shipname:
        name += (" namens " if german else " named ") + msg.shipname
    notice = name + (" manövriert in unmittelbarer Nähe" if german else " manoeuvring close")
    if msg.bearing is not None:
        notice += (
            (", Peilung " if german else ", bearing ")
            + speech_api.str_number(round(msg.bearing))
            + (" Grad" if german else " degrees")
        )
    if msg.distance is not None:
        notice += (
            ", " + distance_words(msg.distance, speech_api, True, language)
            + (" entfernt" if german else " away")
        )
    return notice + ". " + identification(msg, speech_api, language) + "."

def harbour_notice_due(mmsi):
    """Whether a harbour notice for mmsi may be spoken now; records it if so."""
    current_time = time.monotonic()
    called = harbour_notices.get(mmsi)
    if called is not None and current_time - called < HARBOUR_NOTICE_SECONDS:
        return False
    harbour_notices[mmsi] = current_time
    return True

def warn_track(msg, speech_api, language="en"):
    """Speak the urgent part of the warning for one dangerous track when it is due.

    Returns the details text to speak afterwards, or None when nothing was spoken."""
    if not isinstance(msg, CPATrack):
        return None
    msg = advance_track(msg)
    if msg.tcpa is not None and msg.tcpa <= 0:
        log.info("CPA of MMSI %s passed before the warning was spoken", msg.mmsi)
        return None
    if msg.mode == "harbour":
        if harbour_notice_due(msg.mmsi):
            log.info("Harbour notice for MMSI %s", msg.mmsi)
            register_msg.add_msg(
                compose_harbour_notice(msg, speech_api, language), speech_api
            )
        return None
    urgent, details = compose_warning(msg, speech_api, language)
    log.info("Warning generated for MMSI %s", msg.mmsi)
    if settings.VERBOSE:
        print("Bericht generated:", urgent, details)
    if not repeat_call(msg.mmsi, msg.tcpa):
        return None
    log.info("Issuing warning for MMSI %s", msg.mmsi)
    register_msg.add_msg(urgent, speech_api)  # speaks the warning without persistence
    return details

if __name__ == '__main__':
    print("min_to_hr(100) =", min_to_hr(100) )
    print("min_to_hr(200) =", min_to_hr(200) )
    print("nm_to_meters(0.5) =", nm_to_meters(0.5) )
    print("nm_to_meters(-0.5) =", nm_to_meters(-0.5) )
    print("nm_to_meters(-1.5) =", nm_to_meters(-1.5) )
    print("nm_to_meters(1.5) =", nm_to_meters(1.5) )
    home = (settings.OWN_START_LAT, settings.OWN_START_LON)
    nearby = (home[0] - 0.0008, home[1] + 0.0026)
    print(f"bearing{home + nearby} =", bearing(*home, *nearby))
    print(f"distance{home + nearby} =", distance(*home, *nearby))
    print("call_interval(10) =", call_interval(10) )
    print("call_interval(20) =", call_interval(20) )
    print("call_interval(30) =", call_interval(30) )
