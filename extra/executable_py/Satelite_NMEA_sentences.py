#!/usr/bin/env python3
"""
Decode NMEA 0183 sentences into readable text. Stdlib only.

  $xxGSV  satellites in view          $xxGSA  DOP and active satellites
  !AIVDM / !AIVDO  AIS (types 1-5, 11, 18, 19, 21, 24, 27; others show header)

Usage:
    python3 nmea_decode.py '$GPGSV,3,2,10,11,30,228,37,...*72'
    python3 nmea_decode.py nmea.log
    gpspipe -r | python3 nmea_decode.py        # live from gpsd
    nc localhost 10110 | python3 nmea_decode.py  # SignalK/OpenPlotter NMEA TCP
"""
import os
import sys

TALKERS = {
    "GP": "GPS", "GL": "GLONASS", "GA": "Galileo", "GB": "BeiDou",
    "BD": "BeiDou", "GQ": "QZSS", "GN": "Multi-GNSS", "GI": "NavIC",
}
SYSTEM_IDS = {"1": "GPS", "2": "GLONASS", "3": "Galileo", "4": "BeiDou",
              "5": "QZSS", "6": "NavIC"}
FIX_TYPES = {"1": "no fix", "2": "2D fix", "3": "3D fix"}
MODES = {"A": "automatic (2D/3D switching)", "M": "manual (forced)"}


def checksum_ok(line):
    """Return (ok, body, given). body is the text between $/! and *."""
    if not line or line[0] not in "$!" or "*" not in line:
        return False, line[1:], None
    body, given = line[1:].split("*", 1)
    given = given.strip()[:2]
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return f"{calc:02X}" == given.upper(), body, given.upper()


def compass(deg):
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]


def num(s, cast=float):
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


_gsv_parts = {}  # (talker, signal_id) -> {msg_num: [sats]}


def decode_gsv(talker, f):
    system = TALKERS.get(talker, talker)
    total, idx, in_view = num(f[1], int), num(f[2], int), num(f[3], int)
    rest = f[4:]
    signal_id = None
    if len(rest) % 4 == 1:           # NMEA 4.10 trailing signal ID
        signal_id = rest[-1]
        rest = rest[:-1]

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

    key = (talker, signal_id)
    if idx == 1:
        _gsv_parts[key] = {}
    _gsv_parts.setdefault(key, {})[idx] = sats
    if idx == total and len(_gsv_parts[key]) == total:
        allsats = [s for k in sorted(_gsv_parts[key]) for s in _gsv_parts[key][k]]
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
        seg = self.s[a:a + n]
        return int(seg.ljust(n, "0"), 2) if n else 0

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
    bits = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        if not 0 <= v < 64:
            raise ValueError(f"bad payload character {ch!r}")
        bits.append(format(v, "06b"))
    s = "".join(bits)
    return s[:len(s) - fill] if fill else s


def _lon(raw, div):
    return None if raw == 181 * div else raw / div


def _lat(raw, div):
    return None if raw == 91 * div else raw / div


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
    t = b.u(0, 6)
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
    if n == 0:
        return "not available"
    if n in SHIP_SPECIAL:
        return SHIP_SPECIAL[n]
    grp = SHIP_GROUPS.get(n // 10)
    if grp:
        return f"{grp} ({n})"
    return f"type {n}"


def mmsi_hint(m):
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
    if lat is None or lon is None:
        return "not available"

    def dm(v, pos, neg):
        h = pos if v >= 0 else neg
        v = abs(v)
        return f"{int(v)}°{(v - int(v)) * 60:06.3f}' {h}"
    return f"{lat:.5f}, {lon:.5f}  ({dm(lat, 'N', 'S')}, {dm(lon, 'E', 'W')})"


def fmt_ais(d, channel="?"):
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


_ais_frags = {}  # (seq_id, channel) -> {"total": n, "parts": {num: payload}}


def decode_ais(f):
    if len(f) < 7:
        raise ValueError("too few fields")
    total, num_, seq, chan, payload, fill = (
        int(f[1]), int(f[2]), f[3], f[4], f[5], int(f[6] or 0))
    if total == 1:
        return fmt_ais(parse_ais(payload, fill), chan)

    key = (seq, chan)
    if num_ == 1:
        _ais_frags[key] = {"total": total, "parts": {}}
    ent = _ais_frags.get(key)
    if ent is None:
        return decode_orphan(total, num_, seq, chan, payload, fill)
    ent["parts"][num_] = (payload, fill)
    if len(ent["parts"]) < total:
        return None                       # wait for the rest
    del _ais_frags[key]
    joined = "".join(ent["parts"][k][0] for k in sorted(ent["parts"]))
    last_fill = ent["parts"][total][1]
    return fmt_ais(parse_ais(joined, last_fill), chan)


def decode_orphan(total, num_, seq, chan, payload, fill):
    """Fragment arrived without its predecessors."""
    out = [f"AIS  fragment {num_} of {total} (seq {seq or '-'}, channel {chan}) "
           f"without fragment 1, so MMSI/type are unknown"]
    bits = unarmor(payload, fill)
    # Type 5 is 424 bits = 60 chars + 64 bits, a very common 2-part layout
    if total == 2 and num_ == 2 and 360 + len(bits) == 424:
        b = Bits(bits)
        dest_tail = Bits(bits[2:62]).txt(0, 60)
        out.append("  Looks like the tail of a Type 5 message (length matches 424 bits):")
        out.append(f"  Destination (last 10 chars): {dest_tail!r}  DTE: {b.u(62, 1)}")
    return "\n".join(out)


def flush_incomplete():
    out = []
    for (seq, chan), ent in _ais_frags.items():
        got = sorted(ent["parts"])
        out.append(f"AIS  incomplete message (seq {seq or '-'}, channel {chan}): "
                   f"have fragment(s) {got} of {ent['total']}")
    return out


# =============================================================== driver
def decode_line(line):
    line = line.strip()
    start = min((i for i in (line.find("$"), line.find("!")) if i >= 0), default=-1)
    if start < 0:
        return None
    line = line[start:]                      # drop timestamps / tag blocks
    ok, body, given = checksum_ok(line)
    f = body.split(",")
    if len(f[0]) != 5:
        return None
    talker, kind = f[0][:2], f[0][2:]
    try:
        if kind == "GSV" and line[0] == "$":
            text = decode_gsv(talker, f)
        elif kind == "GSA" and line[0] == "$":
            text = decode_gsa(talker, f)
        elif kind in ("VDM", "VDO") and line[0] == "!":
            text = decode_ais(f)
        else:
            return None
    except (IndexError, ValueError) as e:
        return f"{f[0]}: malformed sentence ({e})"
    if text is not None and not ok:
        text += f"\n  !! checksum mismatch (sentence says {given})"
    return text


def main():
    args = sys.argv[1:]
    if args and os.path.isfile(args[0]):
        lines = open(args[0], errors="replace")
    elif args:
        lines = args
    else:
        lines = sys.stdin
    for line in lines:
        res = decode_line(line)
        if res:
            print(res + "\n", flush=True)
    for msg in flush_incomplete():
        print(msg)


if __name__ == "__main__":
    main()
