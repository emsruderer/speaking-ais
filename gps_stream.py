"""GPS and AIS stream from SignalK's NMEA 0183 TCP output (default port 10110).

Yields one dict per decoded sentence. Every dict has a "sentence" key
(GLL, RMC, VTG, ZDA, HDT or AIS). Missing values are None, so a receiver
without a fix never crashes the generator.

Used by ships.py for own-ship navigation. Needs nmea_decode.py in the same
directory for NMEA checksum validation.
Use decode_line for dictionaries and decode_text_line for readable text.
"""
import socket
import sys
import time
from math import isfinite

from nmea_decode import checksum_ok

TALKERS = {
    "GP": "GPS", "GL": "GLONASS", "GA": "Galileo", "GB": "BeiDou",
    "BD": "BeiDou", "GQ": "QZSS", "GN": "Multi-GNSS", "GI": "NavIC",
}
SYSTEM_IDS = {"1": "GPS", "2": "GLONASS", "3": "Galileo", "4": "BeiDou",
              "5": "QZSS", "6": "NavIC"}
FIX_TYPES = {"1": "no fix", "2": "2D fix", "3": "3D fix"}
MODES = {"A": "automatic (2D/3D switching)", "M": "manual (forced)"}


def compass(deg):
    """Convert degrees to compass direction."""
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]


def num(s, cast=float):
    """Convert a string to a number, or return None if invalid."""
    try:
        return cast(s)
    except (ValueError, TypeError):
        return None


# =============================================================== GSV / GSA
def snr_quality(snr):
    if snr is None:
        return "not tracked"
    if snr >= 40:
        return "excellent"
    if snr >= 30:
        return "good"
    if snr >= 20:
        return "weak"
    return "very weak"


def dop_quality(v):
    for limit, name in ((1, "ideal"), (2, "excellent"), (5, "good"),
                        (10, "moderate"), (20, "fair")):
        if v <= limit:
            return name
    return "poor"


class GsvAssembler:
    """Track GSV multipart cycles for one input stream."""

    def __init__(self, timeout=10, max_pending=32):
        if timeout <= 0 or max_pending < 1:
            raise ValueError("timeout and max_pending must be positive")
        self.pending = {}
        self.timeout = timeout
        self.max_pending = max_pending

    def add(self, key, total, idx, sats):
        now = time.monotonic()
        self.pending = {
            k: entry for k, entry in self.pending.items()
            if now - entry["updated"] <= self.timeout
        }
        if idx == 1:
            if key not in self.pending and len(self.pending) >= self.max_pending:
                oldest = min(self.pending, key=lambda k: self.pending[k]["updated"])
                del self.pending[oldest]
                print("GSV assembly limit reached; discarded oldest cycle",
                      file=sys.stderr)
            self.pending[key] = {"total": total, "parts": {}, "updated": now}
        entry = self.pending.get(key)
        if entry is None or entry["total"] != total:
            self.pending.pop(key, None)
            return None
        entry["parts"][idx] = sats
        entry["updated"] = now
        if len(entry["parts"]) != total:
            return None
        del self.pending[key]
        return [sat for part in range(1, total + 1)
                for sat in entry["parts"][part]]


def decode_gsv(talker, f, assembler=None):
    system = TALKERS.get(talker, talker)
    total, idx, in_view = num(f[1], int), num(f[2], int), num(f[3], int)
    if (total is None or idx is None or in_view is None or total < 1
            or idx < 1 or idx > total or in_view < 0):
        raise ValueError("invalid GSV sentence counters")
    rest = f[4:]
    signal_id = None
    if len(rest) % 4 == 1:           # NMEA 4.10 trailing signal ID
        signal_id = rest[-1]
        rest = rest[:-1]
    if len(rest) % 4:
        raise ValueError("incomplete GSV satellite fields")

    sats = []
    for i in range(0, len(rest) - 3, 4):
        prn, el, az, snr = rest[i:i + 4]
        if prn:
            sats.append({"prn": prn, "el": num(el, int),
                         "az": num(az, int), "snr": num(snr, int)})

    out = [f"GSV  {system} satellites in view  "
           f"(sentence {idx} of {total}, {in_view} in view"
           + (f", signal {signal_id}" if signal_id else "") + ")"]
    if sats:
        out.append(f"  {'PRN':>4} {'Elev':>5} {'Azimuth':>10} {'SNR':>7}  Signal")
        for s in sats:
            az = "--" if s["az"] is None else f"{s['az']:3d}° {compass(s['az']):<3}"
            el = "--" if s["el"] is None else f"{s['el']}°"
            snr = "--" if s["snr"] is None else f"{s['snr']} dB"
            flag = ""
            if s["el"] is not None and s["el"] < 10:
                flag = "  (low elevation: multipath likely)"
            out.append(f"  {s['prn']:>4} {el:>5} {az:>10} {snr:>7}  "
                       f"{snr_quality(s['snr'])}{flag}")
    else:
        out.append("  (no satellite data in this sentence)")

    if assembler is not None:
        allsats = assembler.add((talker, signal_id), total, idx, sats)
    else:
        allsats = sats if total == 1 else None
    if allsats is not None:
        snrs = [s["snr"] for s in allsats if s["snr"] is not None]
        out.append(f"  Summary: {len(allsats)} listed, {len(snrs)} with signal"
                   + (f", mean SNR {sum(snrs) / len(snrs):.0f} dB, "
                      f"max {max(snrs)} dB" if snrs else ""))
        for s in allsats:
            if s["el"] and s["el"] >= 60 and s["snr"] is not None and s["snr"] < 25:
                out.append(f"  Note: PRN {s['prn']} is high ({s['el']}°) but weak "
                           f"({s['snr']} dB). Suspect antenna shadowing.")
    return "\n".join(out)


def decode_gsa(talker, f):
    system = TALKERS.get(talker, talker)
    f = f + [""] * (18 - len(f))
    mode = MODES.get(f[1], f[1] or "?")
    fix = FIX_TYPES.get(f[2], f[2] or "?")
    used = [p for p in f[3:15] if p]
    pdop, hdop, vdop = num(f[15]), num(f[16]), num(f[17])
    sysid = f[18] if len(f) > 18 and f[18] else None

    out = [f"GSA  {system} fix status",
           f"  Mode:        {mode}",
           f"  Fix:         {fix}",
           f"  Satellites:  {len(used)} used"
           + (f" (PRN {', '.join(used)})" if used else "")]
    if sysid:
        out.append(f"  System:      {SYSTEM_IDS.get(sysid, sysid)}")
    for name, v, meaning in (("PDOP", pdop, "3D position"),
                             ("HDOP", hdop, "horizontal"),
                             ("VDOP", vdop, "vertical")):
        if v is not None:
            out.append(f"  {name}:        {v:<5} {dop_quality(v)} ({meaning})")
    if f[2] == "1":
        out.append("  Warning: no fix, position is not valid.")
    elif len(used) < 4 and f[2] == "3":
        out.append("  Warning: 3D fix claimed with fewer than 4 satellites.")
    return "\n".join(out)

# =============================================================== AIS
NAV_STATUS = {0: "under way using engine", 1: "at anchor", 2: "not under command",
              3: "restricted manoeuvrability", 4: "constrained by draught",
              5: "moored", 6: "aground", 7: "engaged in fishing",
              8: "under way sailing", 9: "reserved (HSC)", 10: "reserved (WIG)",
              11: "towing astern", 12: "pushing/towing alongside",
              13: "reserved", 14: "AIS-SART / MOB / EPIRB active",
              15: "not defined"}
EPFD = {0: "undefined", 1: "GPS", 2: "GLONASS", 3: "GPS+GLONASS", 4: "Loran-C",
        5: "Chayka", 6: "integrated nav system", 7: "surveyed", 8: "Galileo",
        15: "internal GNSS"}
SHIP_GROUPS = {2: "Wing in ground", 4: "High-speed craft", 6: "Passenger",
               7: "Cargo", 8: "Tanker", 9: "Other"}
SHIP_SPECIAL = {30: "Fishing", 31: "Towing", 32: "Towing (large)",
                33: "Dredging/underwater ops", 34: "Diving ops", 35: "Military",
                36: "Sailing", 37: "Pleasure craft", 50: "Pilot vessel",
                51: "Search and rescue", 52: "Tug", 53: "Port tender",
                54: "Anti-pollution", 55: "Law enforcement", 58: "Medical",
                59: "Non-combatant"}
AIS_NAMES = {1: "Position report (Class A)", 2: "Position report (Class A)",
             3: "Position report (Class A)", 4: "Base station report",
             5: "Static and voyage data (Class A)", 11: "UTC/date response",
             18: "Position report (Class B)", 19: "Extended position report (Class B)",
             21: "Aid-to-navigation report", 24: "Static data (Class B)",
             27: "Long-range position report"}

class Bits:
    def __init__(self, s):
        self.s = s

    def __len__(self):
        return len(self.s)

    def u(self, a, n):
        if a < 0 or n < 0 or a + n > len(self.s):
            raise ValueError(f"AIS bit range {a}:{a + n} exceeds {len(self.s)} bits")
        seg = self.s[a:a + n]
        return int(seg, 2) if n else 0

    def i(self, a, n):
        v = self.u(a, n)
        return v - (1 << n) if v >> (n - 1) else v

    def txt(self, a, n):
        out = []
        for k in range(n // 6):
            v = self.u(a + 6 * k, 6)
            out.append(chr(v + 64) if v < 32 else chr(v))
        return "".join(out).split("@")[0].rstrip()


def unarmor(payload, fill):
    if not 0 <= fill <= 5:
        raise ValueError("AIS fill bits must be between 0 and 5")
    if not payload:
        raise ValueError("empty AIS payload")
    bits = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        if not 0 <= v < 64 or 88 <= ord(ch) <= 95:
            raise ValueError(f"bad payload character {ch!r}")
        bits.append(format(v, "06b"))
    s = "".join(bits)
    if fill and fill > len(s):
        raise ValueError("AIS fill bits exceed payload length")
    return s[:len(s) - fill] if fill else s


def _lon(raw, div):
    value = raw / div
    return None if raw == 181 * div or not -180 <= value <= 180 else value


def _lat(raw, div):
    value = raw / div
    return None if raw == 91 * div or not -90 <= value <= 90 else value


def _dims(b, a):
    return {"to_bow": b.u(a, 9), "to_stern": b.u(a + 9, 9),
            "to_port": b.u(a + 18, 6), "to_starboard": b.u(a + 24, 6)}


def _rot(raw):
    if raw == -128:
        return None
    if abs(raw) == 127:
        return "turning >5°/30s " + ("right" if raw > 0 else "left")
    v = (raw / 4.733) ** 2
    return round(v if raw >= 0 else -v, 1)


def parse_ais(payload, fill):
    b = Bits(unarmor(payload, fill))
    if len(b) < 38:
        raise ValueError("AIS payload is shorter than its common header")
    t = b.u(0, 6)
    required_bits = {
        1: 168, 2: 168, 3: 168, 4: 168, 5: 424, 11: 168,
        18: 168, 19: 312, 24: 160, 27: 96,
    }
    required = required_bits.get(t)
    if required is not None and len(b) < required:
        raise ValueError(f"AIS type {t} requires at least {required} bits")
    if required is not None and t != 24 and len(b) != required:
        raise ValueError(f"AIS type {t} must be exactly {required} bits")
    if t == 21 and (not 272 <= len(b) <= 360 or (len(b) - 272) % 6):
        raise ValueError(f"AIS type 21 has invalid length {len(b)} bits")
    if t == 24:
        part = b.u(38, 2)
        expected = 160 if part == 0 else 168 if part == 1 else None
        if expected is None or len(b) != expected:
            raise ValueError(f"AIS type 24 part {part} has invalid length {len(b)} bits")
    d = {"type": t, "repeat": b.u(6, 2), "mmsi": b.u(8, 30), "bits": len(b)}

    if t in (1, 2, 3):
        sog, cog, hdg = b.u(50, 10), b.u(116, 12), b.u(128, 9)
        d.update(status=b.u(38, 4), turn=_rot(b.i(42, 8)),
                 speed=None if sog == 1023 else sog / 10, accuracy=b.u(60, 1),
                 lon=_lon(b.i(61, 28), 600000), lat=_lat(b.i(89, 27), 600000),
                 course=None if cog == 3600 else cog / 10,
                 heading=None if hdg == 511 else hdg, second=b.u(137, 6))
    elif t in (4, 11):
        d.update(year=b.u(38, 14), month=b.u(52, 4), day=b.u(56, 5),
                 hour=b.u(61, 5), minute=b.u(66, 6), second=b.u(72, 6),
                 accuracy=b.u(78, 1), lon=_lon(b.i(79, 28), 600000),
                 lat=_lat(b.i(107, 27), 600000))
    elif t == 5:
        d.update(imo=b.u(40, 30), callsign=b.txt(70, 42),
                 shipname=b.txt(112, 120), ship_type=b.u(232, 8),
                 epfd=b.u(270, 4), month=b.u(274, 4), day=b.u(278, 5),
                 hour=b.u(283, 5), minute=b.u(288, 6),
                 draught=b.u(294, 8) / 10, destination=b.txt(302, 120),
                 dte=b.u(422, 1), **_dims(b, 240))
    elif t in (18, 19):
        sog, cog, hdg = b.u(46, 10), b.u(112, 12), b.u(124, 9)
        d.update(speed=None if sog == 1023 else sog / 10, accuracy=b.u(56, 1),
                 lon=_lon(b.i(57, 28), 600000), lat=_lat(b.i(85, 27), 600000),
                 course=None if cog == 3600 else cog / 10,
                 heading=None if hdg == 511 else hdg, second=b.u(133, 6))
        if t == 19:
            d.update(shipname=b.txt(143, 120), ship_type=b.u(263, 8),
                     epfd=b.u(301, 4), **_dims(b, 271))
    elif t == 21:
        d.update(aid_type=b.u(38, 5), name=b.txt(43, 120), accuracy=b.u(163, 1),
                 lon=_lon(b.i(164, 28), 600000), lat=_lat(b.i(192, 27), 600000),
                 epfd=b.u(249, 4), off_position=b.u(259, 1),
                 virtual=b.u(269, 1), **_dims(b, 219))
    elif t == 24:
        part = b.u(38, 2)
        d["part"] = part
        if part == 0:
            d["shipname"] = b.txt(40, 120)
        elif part == 1:
            d.update(ship_type=b.u(40, 8), vendor=b.txt(48, 18),
                     callsign=b.txt(90, 42))
            if str(d["mmsi"]).startswith("98"):
                d["mothership_mmsi"] = b.u(132, 30)
            else:
                d.update(_dims(b, 132))
    elif t == 27:
        sog, cog = b.u(79, 6), b.u(85, 9)
        d.update(accuracy=b.u(38, 1), status=b.u(40, 4),
                 lon=_lon(b.i(44, 18), 600), lat=_lat(b.i(62, 17), 600),
                 speed=None if sog == 63 else float(sog),
                 course=None if cog == 511 else float(cog))
    return d


def ship_type_text(n):
    """Return a human-readable description of a ship type code."""
    if n == 0:
        return "not available"
    if n in SHIP_SPECIAL:
        return SHIP_SPECIAL[n]
    grp = SHIP_GROUPS.get(n // 10)
    if grp:
        return f"{grp} ({n})"
    return f"type {n}"


def mmsi_hint(m):
    """Return a human-readable hint about the type of vessel based on the MMSI."""
    s = f"{m:09d}"
    if s.startswith("970"):
        return "AIS-SART"
    if s.startswith("972"):
        return "MOB device"
    if s.startswith("974"):
        return "EPIRB-AIS"
    if s.startswith("99"):
        return "aid to navigation"
    if s.startswith("98"):
        return "auxiliary craft"
    if s.startswith("00"):
        return "coast station"
    return None


def fmt_pos(lat, lon):
    """Format latitude and longitude as a human-readable string."""
    if lat is None or lon is None:
        return "not available"

    def dm(v, pos, neg):
        h = pos if v >= 0 else neg
        v = abs(v)
        return f"{int(v)}°{(v - int(v)) * 60:06.3f}' {h}"
    return f"{lat:.5f}, {lon:.5f}  ({dm(lat, 'N', 'S')}, {dm(lon, 'E', 'W')})"


def fmt_ais(d, channel="?"):
    """Format an AIS message dictionary into a human-readable string."""
    t, m = d["type"], d["mmsi"]
    hint = mmsi_hint(m)
    out = [f"AIS  type {t}: {AIS_NAMES.get(t, 'not decoded here')}  "
           f"MMSI {m}" + (f" ({hint})" if hint else "") + f"  channel {channel}"]

    def add(k, v):
        out.append(f"  {k:<13}{v}")

    def sog(v):
        return "n/a" if v is None else f"{v:g} kn"

    def deg(v):
        return "n/a" if v is None else f"{v:g}°"

    def dims(d):
        if "to_bow" not in d or not (d["to_bow"] or d["to_stern"]):
            return "n/a"
        return (f"{d['to_bow'] + d['to_stern']} m x {d['to_port'] + d['to_starboard']} m"
                f"  (bow {d['to_bow']}, stern {d['to_stern']}, "
                f"port {d['to_port']}, stbd {d['to_starboard']})")

    if t in (1, 2, 3):
        add("Status", NAV_STATUS.get(d["status"], d["status"]))
        add("Position", fmt_pos(d["lat"], d["lon"]))
        add("SOG / COG", f"{sog(d['speed'])} / {deg(d['course'])}")
        add("Heading", deg(d["heading"]))
        if d["turn"] is not None:
            add("Rate of turn", d["turn"] if isinstance(d["turn"], str)
                else f"{d['turn']}°/min")
        add("Timestamp", f"second {d['second']}" if d["second"] < 60 else "n/a")
    elif t in (4, 11):
        add("UTC", f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d} "
                   f"{d['hour']:02d}:{d['minute']:02d}:{d['second']:02d}")
        add("Position", fmt_pos(d["lat"], d["lon"]))
    elif t == 5:
        add("Name", d["shipname"] or "n/a")
        add("Call sign", d["callsign"] or "n/a")
        add("IMO", d["imo"] or "n/a")
        add("Ship type", ship_type_text(d["ship_type"]))
        add("Size", dims(d))
        add("Draught", f"{d['draught']:g} m")
        add("Destination", d["destination"] or "n/a")
        eta = (f"{d['month']:02d}-{d['day']:02d} {d['hour']:02d}:{d['minute']:02d} UTC"
               if d["month"] and d["day"] and d["hour"] < 24 and d["minute"] < 60
               else "n/a")
        add("ETA", eta)
        add("Fix device", EPFD.get(d["epfd"], d["epfd"]))
    elif t in (18, 19):
        add("Position", fmt_pos(d["lat"], d["lon"]))
        add("SOG / COG", f"{sog(d['speed'])} / {deg(d['course'])}")
        add("Heading", deg(d["heading"]))
        if t == 19:
            add("Name", d["shipname"] or "n/a")
            add("Ship type", ship_type_text(d["ship_type"]))
            add("Size", dims(d))
    elif t == 21:
        add("Name", d["name"] or "n/a")
        add("Aid type", d["aid_type"])
        add("Position", fmt_pos(d["lat"], d["lon"]))
        add("Size", dims(d))
        if d["off_position"]:
            add("Warning", "OFF POSITION")
        if d["virtual"]:
            add("Note", "virtual aid (no physical object)")
    elif t == 24:
        if d["part"] == 0:
            add("Name", d["shipname"] or "n/a")
        elif d["part"] == 1:
            add("Ship type", ship_type_text(d["ship_type"]))
            add("Call sign", d["callsign"] or "n/a")
            add("Vendor", d["vendor"] or "n/a")
            if "mothership_mmsi" in d:
                add("Mothership", d["mothership_mmsi"])
            else:
                add("Size", dims(d))
    elif t == 27:
        add("Status", NAV_STATUS.get(d["status"], d["status"]))
        add("Position", fmt_pos(d["lat"], d["lon"]))
        add("SOG / COG", f"{sog(d['speed'])} / {deg(d['course'])}")
    return "\n".join(out)


def _num(s, cast=float):
    """Convert a string to a number, or return None if invalid."""
    try:
        value = cast(s)
    except (ValueError, TypeError):
        return None
    if cast is float and not isfinite(value):
        return None
    return value


def _latlon(value, hemi, deg_digits):
    """ddmm.mmmm / dddmm.mmmm plus hemisphere to signed decimal degrees."""
    if not value or not hemi:
        return None
    try:
        deg, minutes = float(value[:deg_digits]), float(value[deg_digits:])
    except (ValueError, TypeError):
        return None
    if not isfinite(deg) or not isfinite(minutes) or not 0 <= minutes < 60:
        return None
    limit = 90 if deg_digits == 2 else 180
    if deg < 0 or deg > limit or (deg == limit and minutes != 0):
        return None
    if hemi not in (("N", "S") if deg_digits == 2 else ("E", "W")):
        return None
    v = deg + minutes / 60.0
    return -v if hemi in ("S", "W") else v


def decode_gll(f):
    # GLL - Geographic Position, Latitude/Longitude
    if len(f) < 7:
        raise ValueError("Invalid GLL sentence")
    valid = f[6] == "A"
    return {"sentence": "GLL",
            "latitude": _latlon(f[1], f[2], 2) if valid else None,
            "longitude": _latlon(f[3], f[4], 3) if valid else None,
            "time_utc": f[5] or None,
            "status": f[6] or None}


def decode_rmc(f):
    # RMC - Recommended Minimum Navigation Information
    if len(f) < 10:
        raise ValueError("Invalid RMC sentence")
    valid = f[2] == "A"
    return {"sentence": "RMC",
            "time_utc": f[1] or None,
            "status": f[2] or None,
            "latitude": _latlon(f[3], f[4], 2) if valid else None,
            "longitude": _latlon(f[5], f[6], 3) if valid else None,
            "speed_over_ground": _num(f[7]),
            "course_over_ground": _num(f[8]),
            "date": f[9] or None,
            # NMEA 2.3+: A autonomous, D differential (SBAS/EGNOS, DGPS), E estimated
            "mode": (f[12] or None) if len(f) > 12 else None}


def decode_vtg(f):
    # VTG - Course over ground and ground speed
    if len(f) < 8:
        raise ValueError("Invalid VTG sentence")
    return {"sentence": "VTG",
            "true_track": _num(f[1]),
            "magnetic_track": _num(f[3]),     # many receivers leave this empty
            "speed_knots": _num(f[5]),
            "speed_kmh": _num(f[7])}


def decode_zda(f):
    # ZDA - Time & Date
    if len(f) < 5:
        raise ValueError("Invalid ZDA sentence")
    return {"sentence": "ZDA", "time_utc": f[1] or None,
            "day": _num(f[2], int), "month": _num(f[3], int),
            "year": _num(f[4], int)}


def decode_hdt(f):
    # HDT - Heading True
    if len(f) < 2:
        raise ValueError("Invalid HDT sentence")
    return {"sentence": "HDT", "heading": _num(f[1])}


SENTENCES = {"GLL": decode_gll, "RMC": decode_rmc, "VTG": decode_vtg,
             "ZDA": decode_zda, "HDT": decode_hdt}

class AisAssembler:
    """Collect AIS fragments for one input stream and return parsed messages."""

    def __init__(self, max_pending=32, timeout=10):
        if max_pending < 1 or timeout <= 0:
            raise ValueError("max_pending and timeout must be positive")
        self.pending = {}
        self.max_pending = max_pending
        self.timeout = timeout

    def clear(self):
        self.pending.clear()

    def _expire(self):
        now = time.monotonic()
        expired = [key for key, entry in self.pending.items()
                   if now - entry["updated"] > self.timeout]
        for key in expired:
            del self.pending[key]

    def feed(self, f):
        if len(f) < 7:
            raise ValueError("too few AIS fields")
        total, idx = int(f[1]), int(f[2])
        seq, chan, payload, fill = f[3], f[4], f[5], int(f[6] or 0)
        if total < 1 or total > 9 or idx < 1 or idx > total:
            raise ValueError("invalid AIS fragment counters")
        if not 0 <= fill <= 5 or not payload:
            raise ValueError("invalid AIS fill bits or empty payload")
        if total == 1:
            if idx != 1:
                raise ValueError("single-fragment AIS message must be fragment 1")
            parts = {1: (payload, fill)}
        else:
            self._expire()
            sentence = f[0]
            key = (seq, chan, sentence)
            if idx == 1:
                if key not in self.pending and len(self.pending) >= self.max_pending:
                    oldest = min(self.pending, key=lambda k: self.pending[k]["updated"])
                    del self.pending[oldest]
                    print("AIS fragment assembly limit reached; discarded oldest message",
                          file=sys.stderr)
                self.pending[key] = {
                    "total": total, "parts": {}, "updated": time.monotonic()
                }
            entry = self.pending.get(key)
            if entry is None:
                return None
            if entry["total"] != total:
                del self.pending[key]
                raise ValueError("AIS fragment count changed during assembly")
            parts = entry["parts"]
            previous = parts.get(idx)
            fragment = (payload, fill)
            if previous is not None and previous != fragment:
                del self.pending[key]
                raise ValueError("conflicting duplicate AIS fragment")
            parts[idx] = fragment
            entry["updated"] = time.monotonic()
            if set(parts) != set(range(1, total + 1)):
                return None
            del self.pending[key]
        joined = "".join(parts[k][0] for k in sorted(parts))
        if any(parts[k][1] for k in range(1, max(parts))):
            raise ValueError("AIS fill bits are only valid on the final fragment")
        d = parse_ais(joined, parts[max(parts)][1])
        return {"sentence": "AIS", "channel": chan, **d}


def _sentence_fields(line):
    line = line.strip()
    start = min((i for i in (line.find("$"), line.find("!")) if i >= 0), default=-1)
    if start < 0:
        return None, None, None, "no NMEA sentence marker"
    line = line[start:]
    ok, body, _ = checksum_ok(line)
    if not ok:
        return line, None, None, "bad or missing checksum"
    fields = body.split(",")
    if len(fields[0]) != 5:
        return line, None, None, "invalid sentence identifier"
    return line, fields, fields[0][2:], None


def decode_line(line, ais=None, verbose=False):
    """Decode a sentence to a dictionary, or return None if invalid or incomplete."""
    line, fields, kind, error = _sentence_fields(line)
    if error:
        if verbose:
            print(f"Invalid NMEA line ({error}): {line or ''}", file=sys.stderr)
        elif line and error == "bad or missing checksum":
            print(f"Bad checksum, dropped: {line}", file=sys.stderr)
        return None
    try:
        if line.startswith("$") and kind in SENTENCES:
            return SENTENCES[kind](fields)
        if line.startswith("!") and kind in ("VDM", "VDO"):
            if ais is None:
                total = int(fields[1])
                if total != 1:
                    if verbose:
                        print("AIS fragments require a persistent AisAssembler",
                              file=sys.stderr)
                    return None
                ais = AisAssembler()
            return ais.feed(fields)
    except (IndexError, TypeError, ValueError) as e:
        print(f"Malformed {fields[0]}: {e}", file=sys.stderr)
        return None
    if verbose:
        print(f"Unhandled line: {line}", file=sys.stderr)
    return None


def decode_text_line(line, ais=None, gsv=None, verbose=False):
    """Decode a sentence to human-readable text; supports GSV/GSA and decoded data."""
    line, fields, kind, error = _sentence_fields(line)
    if error:
        if verbose:
            print(f"Invalid NMEA line ({error}): {line or ''}", file=sys.stderr)
        return None
    try:
        if line.startswith("$") and kind == "GSV":
            return decode_gsv(fields[0][:2], fields, gsv)
        if line.startswith("$") and kind == "GSA":
            return decode_gsa(fields[0][:2], fields)
        if line.startswith("!") and kind in ("VDM", "VDO"):
            decoded = decode_line(line, ais=ais, verbose=verbose)
            return fmt_ais(decoded, decoded["channel"]) if decoded else None
        decoded = decode_line(line, ais=ais, verbose=verbose)
        if decoded is None:
            return None
        sentence = decoded.pop("sentence")
        return f"{sentence}  " + ", ".join(
            f"{key.replace('_', ' ')}: {value if value is not None else 'n/a'}"
            for key, value in decoded.items()
        )
    except (IndexError, TypeError, ValueError) as e:
        print(f"Malformed {fields[0]}: {e}", file=sys.stderr)
        return None


class NmeaTextDecoder:
    """Stateful text decoder for AIS and GSV multipart messages."""

    def __init__(self):
        self.ais = AisAssembler()
        self.gsv = GsvAssembler()

    def decode_line(self, line, verbose=False):
        return decode_text_line(line, self.ais, self.gsv, verbose)

    def clear(self):
        self.ais.clear()
        self.gsv.pending.clear()


def gps_from_signalk(host="localhost", port=10110, reconnect=True,
                     timeout=30, verbose=False, max_line_bytes=4096):
    """Generator yielding decoded dicts from a SignalK NMEA 0183 TCP server."""
    if max_line_bytes < 1:
        raise ValueError("max_line_bytes must be positive")
    ais = AisAssembler()
    while True:
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                print(f"Connected to SignalK server at {host}:{port}", file=sys.stderr)
                buffer = b""
                discarding_line = False
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    if discarding_line:
                        newline = chunk.find(b"\n")
                        if newline < 0:
                            continue
                        chunk = chunk[newline + 1:]
                        discarding_line = False
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if len(raw) > max_line_bytes:
                            print("NMEA line exceeds configured size limit; dropped",
                                  file=sys.stderr)
                            continue
                        msg = decode_line(
                            raw.decode("ascii", "replace").strip(),
                            ais=ais,
                            verbose=verbose,
                        )
                        if msg:
                            yield msg
                    if len(buffer) > max_line_bytes:
                        print("NMEA line exceeds configured size limit; dropping until newline",
                              file=sys.stderr)
                        buffer = b""
                        discarding_line = True
        except OSError as e:               # includes timeouts and refused
            print(f"Connection problem: {e}", file=sys.stderr)
        ais.clear()
        if not reconnect:
            return
        time.sleep(5)


if __name__ == "__main__":
    print("Starting GPS stream from SignalK server...", file=sys.stderr)
    for message in gps_from_signalk(verbose=True):
        print(message)
