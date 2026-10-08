# AIS collision watcher

A talking AIS watcher for a small vessel. It reads AIS and GPS data from a
SignalK server's NMEA 0183 TCP output, keeps a track of every vessel in
range, calculates the closest point of approach (CPA) and time to CPA (TCPA)
against the own ship, and announces dangerous encounters by voice. A Tk
window shows all tracked vessels and the dangerous ones.

The own ship defaults to MMSI 244030153 at Jemgum (53.264282 N, 7.397336 E).

## Running

| Command | What it does |
|---|---|
| `python3 ais_stream.py [--open \| --river]` | Main program: tracking plus spoken warnings (warnings run in a separate process). Uses the river/channel thresholds by default; `--open` selects the open water thresholds. |
| `python3 -m extra.ais_message_window [--host H] [--port P] [--own-lat ..] [--own-lon ..] [--own-course ..] [--own-speed ..]` | Optional GUI with "All tracked ships" and "Dangerous ships" tables. No speech. |
| `python3 multi_process_ais.py` | Same tracking and spoken warnings as `ais_stream.py`, but as three processes (reader, decoder, warning), and only for vessels within 12 NM. Add `--monitor` to open a window showing approximate queue depth, cumulative messages added/removed, and worker status. |
| `python3 -m extra.ais_stream_threads` | Experimental console receiver: prints CPA data of vessels within 15 km every 10 s. |
| `python3 nmea_decode.py <sentence \| file>` or `nc localhost 10110 \| python3 nmea_decode.py` | Human-readable decoding of GSV/GSA and AIS sentences, for diagnostics. |
| `python3 gps_stream.py` | Prints decoded GPS/AIS dictionaries from the SignalK stream. Also supplies own-ship GPS decoding to both AIS programs. |

All programs connect to `localhost:10110` by default (the SignalK NMEA 0183
TCP server). The one stream carries both GPS (`$..RMC`, `$..GLL`, ...) and AIS
(`!AIVDM`) sentences.

### Dependencies

- Python 3.12
- `pyais` (vendored in `pyais/`)
- Speech: `kokoro-onnx`, `onnxruntime`, `sounddevice`, plus the
  model files `kokoro-v1.0.onnx` and `voices-v1.0.bin` next to `speech.py`.
  On a Raspberry Pi also add `kokoro-v1.0.int8.onnx`; it is used when present
  and is much faster. ONNX Runtime uses all CPU cores.
- The generated speech waveform is kept in memory only. A representative
  30.63-second warning at 24 kHz float32 used 2.80 MiB for its waveform;
  playback-backend buffers and the TTS model are additional.
- GUI: `tkinter`

## Architecture

```
                 SignalK NMEA 0183 TCP :10110
                 │                          │
       $GPRMC/GLL/VTG/ZDA/HDT          !AIVDM (types 1,2,3,5,18,19,24)
                 │                          │
        ┌────────▼────────┐        ┌────────▼─────────┐
        │ Ship (thread)   │        │ do_track         │
        │ ships.py        │◄───────│ ais_stream.py    │
        │ own position,   │  own   │ CPATracker,      │
        │ dead reckoning  │  state │ cpa_tcpa, danger │
        └─────────────────┘        └───┬──────────┬───┘
                                       │          │ track_callback
                      dangerous tracks │          ▼
                    (multiprocessing                       │   extra/ais_message_window.py (optional)
                     Queue, max 100)   │   (Tk tables)
                                ┌──────▼───────┐
                                │ do_warn      │
                                │ warn.py      │  separate process
                                │ (compose     │
                                │  message)    │
                                └──────┬───────┘
                                       │
                         register_msg.add_msg ─► in-memory TTS playback
                                       │
                                       ▼
                                speech.py (Kokoro TTS, play)
```

### Processing flow

1. **Own ship** (`Ship` thread) opens its own connection to the stream, uses
   only `$` navigation sentences and updates position, speed over ground (SOG),
   course over ground (COG), heading and time. If no valid fix arrives for 10 s
   (`GPS_STALE_SECONDS`), or the stream fails, it switches to dead reckoning
   from the last valid fix using the last COG/SOG.
2. **Tracking** (`do_track`) decodes AIS with pyais and keeps one `CPATrack`
   per MMSI in a `CPATracker` (tracks expire after 600 s without messages).
   - Position reports (1, 2, 3, 18): CPA/TCPA is calculated against the own
     ship and stored in the track.
   - Static data (5, 19, 24): name, call sign, type, dimensions and destination
     are merged into the track.
   - Every 5 s (`CPA_REFRESH_SECONDS`) the CPA of *all* tracks is
     recalculated. Each target is projected forward from its last report, so
     the warning stays current between reports and as the own ship moves.
3. **Danger test** (`danger`, `dangerous_track`): with the open profile (`--open`) a track is dangerous when all of the following hold; the default river profile uses the limits under *River / channel mode*:
   - `0 < TCPA < 30` minutes (`MINIMUM_TCPA`); the last seconds before CPA count
   - `CPA < 1` NM (`MINIMUM_CPA`)
   - current distance `< 5` NM (`MINIMUM_DISTANCE`) **or** `TCPA < 10` minutes
     (`ALWAYS_WARN_TCPA`), so fast vessels get at least 10 minutes' warning
     (a 45 kn RIB head-on is announced at 8.5 NM instead of 4.9 NM)

   Slow class B vessels (message types 18, 19, 24 with a known speed of at
   most 7 kn) are relaxed: dangerous only when `CPA < 0.3` NM and
   `0 < TCPA < 10` minutes.

   The CPA limits apply to the **hull-to-hull clearance**: the antenna CPA minus
   the target's wider side (AIS `to_port`/`to_starboard`, 3 m when unknown)
   and the own half beam plus antenna offset (`OWN_BEAM_M` 3.5 m,
   `OWN_ANTENNA_OFFSET_M` 0 in `settings.py`). The spoken CPA is this clearance.

   A vessel counts as stationary without a known course or speed, below
   0.5 kn (`STATIONARY_SPEED`), or with status moored / at anchor below 1 kn.
   The own vessel counts as stationary below 0.5 kn. At rest GPS and AIS report
   0.1–0.3 kn with a random course, which otherwise fakes relative motion.

### Position accuracy

AIS positions can be about 9 m off for each vessel (GPS without correction),
and the error is a slowly drifting offset, so averaging does not remove it.
With 2 × 9 m the apparent CPA can be 18 m off: with the 50 m river limit a
true CPA below about 32 m is always announced and a passing beyond about 68 m
never, in between it depends on the errors. River and harbour warnings
therefore speak distances rounded to 10 m as "about …".

Only the own error can be reduced: use a GPS with SBAS (EGNOS) correction and
an antenna clear of mast and rigging. `Ship` logs the RMC mode indicator:
`A` (autonomous, about 9 m) as a warning, `D` (differential/SBAS) as info.

### Mooring and manoeuvring

The program is meant to be shut down once moored; while mooring, the skipper
should not be distracted by warnings about the boats around the berth. While
the own vessel makes less than 3 kn (`MANOEUVRE_SPEED`, until 3.5 kn again;
`QUIET_MANOEUVRING` in `settings.py`):

- stationary vessels (moored, at anchor, below 0.5 kn) are ignored;
- slow vessels (1–3 kn, also manoeuvring) get the harbour profile: one notice,
  at most every 10 minutes per vessel, when passing within 15 m hull clearance
  inside 0.1 NM:

  > Tug vessel named TUG manoeuvring close, bearing ninety degrees, about
  > seventy meters away. Call sign Delta Charlie three.
- vessels making 3 kn or more keep the normal open or river warnings, so
  fairway traffic is still announced when waiting, at anchor or entering.

Simulated mooring (10 minutes: entering a marina lane at 2.5 kn, the last 50 m
at 1 kn, 20 moored boats 8–15 m to either side, one boat leaving at 2 kn,
2 x 9 m position errors):

| | moored boats | boat leaving | river traffic 150 m off |
|---|---|---|---|
| open, quiet manoeuvring off | 22.8 | 4.0 | 8.2 |
| open, quiet manoeuvring on | 0 | 0.6 | 8.1 |
| river, quiet manoeuvring on | 0 | 0.6 | 0 |

In a harbour on a river keep the default river profile: the open profile
(`--open`) announces vessels in the fairway outside the harbour.

### River / channel mode

In rivers and narrow channels vessels normally pass at 30–100 m, so the open
water limits would announce every passing vessel. The river profile
(`ais_stream.PROFILES`) is therefore the default (`NAVIGATION_MODE = "river"`
in `settings.py`). On open water or wide estuaries start any program with
`--open`, or set `NAVIGATION_MODE = "open"`; `--river` selects the river
profile when the setting is changed:

| | open (`--open`) | river (default) |
|---|---|---|
| CPA limit | 1 NM | 50 m |
| Range | < 5 NM, or TCPA < 10 min | < 0.5 NM |
| Slow class B CPA / TCPA | 0.3 NM / 10 min | 25 m / 5 min |
| Target velocity | last report | average of the last 5 reports |
| Warning after | 1 dangerous report | 2 consecutive dangerous reports |

Why these values: AIS course over ground jitters by a few degrees, and 2° is
65 m sideways at 1 NM, so a 50 m CPA cannot be told from a normal 70 m passing
much beyond 0.5 NM. In simulations of normal port-to-port passings at 70 m
(COG noise 2°, position noise 5 m) the plain 50 m limit warned on every
passing; the river profile warns on 2–6 % of them and still detects every
collision course (CPA 0 or 25 m). The price is warning time: about 2 minutes
for an 8 kn vessel, but only about 40 s for a 40 kn vessel.
4. **Warning hand-off**: a copy of a dangerous track goes on the warning queue
   without blocking. If the warning process falls behind and the queue is full,
   the warning is dropped and logged; the next report queues it again.
5. **Warning** (`do_warn`): the most urgent vessel (lowest TCPA) is announced
   first, in two parts. The urgent part gives ship type and name, CPA, TCPA,
   distance, approach speed and call sign:

   > An approaching cargo vessel named JOHANNA. Closest point of approach seven
   > hundred forty one meters, in ten minutes. Currently two nautical miles
   > away, at an approach speed of four knots. Call sign Papa Delta one two
   > three four.

   The details part follows only when no other queued vessel is due for a
   warning (newer reports of the vessel just announced do not count):

   > Details: a Dutch cargo vessel, making six knots, on course one hundred
   > twenty degrees, bearing one hundred eighty degrees. Bound for rotterdam.
   > MMSI two four four zero three zero one five three.

   Distances are spoken in meters below one nautical mile and in nautical
   miles otherwise. TCPA is spoken in seconds below two minutes.

   Just before speaking, CPA data is moved forward from the moment it was
   computed (constant course and speed), so queueing and speech synthesis do
   not make the spoken distance stale; a CPA that has passed by then is not
   spoken.

   Vessels faster than 20 kn or of type high-speed craft (40–49) are announced
   with their speed, and their call sign is always spoken (the MMSI when no
   call sign is known):

   > An approaching fast passenger vessel, at thirty five knots. Closest point
   > of approach three hundred seventy meters, in seven minutes. ... MMSI two
   > one one zero zero zero zero zero one.

   Within one minute of CPA a short danger form is spoken, without details:

   > Danger! Fast vessel named SWIFT close, at forty knots, bearing forty five
   > degrees. Closest approach ninety three meters in thirty five seconds. Call
   > sign Delta Bravo two.

   `repeat_call` limits repeats per vessel: the next announcement is due after
   half the TCPA of the previous one, or at once when the TCPA drops below half
   of what was predicted (a vessel turned or sped up). When speech falls
   behind, the queued backlog is reduced to the newest track per vessel and the
   lowest TCPA is spoken first. The message and generated waveform stay in
   memory and are not written to disk.

### Reliability

- `ais_stream.py` reconnects 5 s after the stream fails or closes, including at
  boot before SignalK is up. A stream silent for 60 s (`STREAM_TIMEOUT_SECONDS`)
  counts as failed; SignalK's GPS sentences normally arrive every second.
- AIS sentences that pyais cannot decode are logged and skipped.
- A dead warning process is restarted within 5 s (`ais_stream.py` and
  `multi_process_ais.py`).
- Tracking and warning intervals use a monotonic clock, so system clock steps
  do not affect them.

### GPS and AIS loss alerts

`LinkMonitor` (`link_monitor.py`) runs in the tracking process of
`ais_stream.py` and `multi_process_ais.py` and checks every 5 s:

| Condition | Spoken |
|---|---|
| No valid GPS fix for 30 s (`GPS_LOST_SECONDS`) | "Warning: GPS position lost. Collision warnings use dead reckoning." |
| No AIS sentence of any type for 10 min (`AIS_LOST_SECONDS`) | "Warning: AIS data lost. No vessel reports are received." |
| Both at the same time | "Warning: GPS and AIS data lost. Check the SignalK connection." |
| Data back | "GPS position restored." / "AIS data restored." |

A lost alert is repeated every 10 min (`ALERT_REPEAT_SECONDS`) while the data
stays lost. Alerts are spoken before any vessel warning and also hold back
vessel details. AIS can be silent for minutes in quiet waters, so lower
`AIS_LOST_SECONDS` only where traffic or an AIS base station is always in range.

### System time from GPS

The Raspberry Pi has no real-time clock. When a valid RMC fix differs more than
5 s (`CLOCK_TOLERANCE_SECONDS`) from the system clock, `Ship` sets the clock to
GPS time. Dates before 2025 are ignored (GPS week rollover). This needs
`CAP_SYS_TIME`, for example in the systemd unit:

```
[Service]
AmbientCapabilities=CAP_SYS_TIME
```

Without it a warning is logged once and the clock is left alone. Turn it off with
`SET_SYSTEM_CLOCK = False` in `settings.py`.

## Modules

| File | Purpose |
|---|---|
| `ais_stream.py` | Tracking: `track_messages` (shared with `multi_process_ais.py`) and `do_track`, which connects to the stream and calls it. Also the danger test, CPA refresh (`refresh_cpa`, `project_position`), non-blocking warning queue (`queue_warning`), program entry point with process shutdown handling. |
| `cpa_tracker.py` | `CPATrack` dataclass (AIS fields plus `cpa`, `tcpa`, `distance`, `bearing`, `approaching_speed`, `position_time`) and `CPATracker`, a pyais `AISTracker` subclass. `available()` turns AIS "not available" values into `None`. |
| `cpa_tcpa.py` | `cpa_tcpa()`: CPA/TCPA between two moving vessels on a local flat-earth projection. |
| `ships.py` | `Ship` thread: own-ship navigation state from the NMEA stream with dead-reckoning fallback; thread-safe getters. |
| `gps_stream.py` | Stdlib NMEA decoder: GLL, RMC, VTG, ZDA, HDT to dictionaries; AIS fragment assembly and bit-level AIS parsing; checksum validation. |
| `nmea_decode.py` | Command-line, human-readable NMEA/AIS decoder (also supplies `checksum_ok`). |
| `warn.py` | Warning process: composes the spoken message from a track, rate-limits repeats, records and speaks it. |
| `speech.py` | Kokoro TTS on CPU; number-to-words (`str_number`) and NATO phonetic spelling (`spell_callsign`). Plays generated float32 audio from memory. Loads the model when the warning process starts. |
| `register_msg.py` | Sends each warning directly to speech playback without saving text or audio. |
| `multi_process_ais.py` | Three-process variant. The reader process queues raw AIS messages and reconnects after errors. The decoder process owns the `Ship` thread, applies the shared AIS range filter and calls `track_messages`. The warning process runs `do_warn`. Ctrl‑C shuts all three down. |
| `extra/ais_message_window.py` | Optional Tk GUI running `do_track` in a reader thread; reconnects every 5 s after a stream error. |
| `extra/ais_stream_threads.py` | Experimental threaded console receiver with a radius filter; forgets vessels after 600 s. |
| `extra/geo_calcule.py` | General great-circle helpers (haversine, bearing, spherical law of cosines, alternative CPA), kept separate from the main pipeline. |
| `link_monitor.py` | `LinkMonitor` thread: spoken alerts when GPS or AIS data is lost or restored. |
| `nationality.py` | MMSI prefix (MID) to country code and country name. |
| `shipstype.py` | AIS ship type code (0–99) to description. |

## Calculations and units

- **Units:** distances in nautical miles, speeds in knots, times in minutes,
  angles in degrees true.
- **CPA/TCPA** (`cpa_tcpa.py`): positions are converted to a local east/north
  plane in NM, using the cosine of the mean latitude. Relative velocity is
  `v_target − v_own`.
  - TCPA = `(d · dv) / |dv|²`, where `d` = own − target.
  - A negative TCPA means the closest point has been passed.
  - With no relative motion, TCPA is infinity and CPA equals the current distance.
- **Bearing** is from the own ship to the target. `approaching_speed` is the
  rate at which the range closes (positive = getting closer).
- **"Not available" AIS values** (lat 91, lon 181, speed 102.3, course 360,
  heading 511) are discarded. A position report without a valid position is
  ignored. An unknown target speed or course is treated as 0, so the target
  counts as stationary.
- **Dead reckoning** moves the own ship along COG at SOG:
  - Δlat = d·cos(COG)/60
  - Δlon = d·sin(COG)/(60·cos lat)

## Configuration

Settings are module constants:

| Constant | File | Default |
|---|---|---|
| `HOST` | `settings.py` | `localhost` (SignalK hostname/IP) |
| `PORT` | `ais_stream.py`, others | `10110` |
| `MINIMUM_CPA`, `MINIMUM_TCPA`, `MINIMUM_DISTANCE` | `ais_stream.py` | 1 NM, 30 min, 5 NM |
| `CPA_REFRESH_SECONDS` | `ais_stream.py` | 5 |
| `ALWAYS_WARN_TCPA` | `ais_stream.py` | 10 min |
| `CLASS_B_SLOW_SPEED`, `CLASS_B_SLOW_CPA`, `CLASS_B_SLOW_TCPA` | `ais_stream.py` | 7 kn, 0.3 NM, 10 min |
| `FAST_SPEED`, `CLOSE_TCPA`, `SECONDS_TCPA` | `warn.py` | 20 kn, 1 min, 2 min |
| `MAX_MESSAGES` (warning queue size) | `ais_stream.py` | 100 |
| `GPS_STALE_SECONDS` | `ships.py` | 10 |
| `RANGE_KM`, `SHIP_TTL_SECONDS` | `ais_stream_threads.py` | 15 km, 600 s |
| `VOICE`, `LANGUAGE`, `SPEED` | `speech.py` | `af_heart`, `en-us`, 0.8 |
| `SET_SYSTEM_CLOCK` | `settings.py` | `True` |
| `NAVIGATION_MODE` | `settings.py` | `"river"` (`--open` / `--river` override it) |
| `QUIET_MANOEUVRING` | `settings.py` | `True` |
| `OWN_BEAM_M`, `OWN_ANTENNA_OFFSET_M` | `settings.py` | 3.5 m, 0 m |
| `AIS_RANGE_NM` | `settings.py` | 12 NM (`None` disables the range filter) |
| `STATIONARY_SPEED`, `MANOEUVRE_SPEED`, `MANOEUVRE_LEAVE_SPEED` | `ais_stream.py` | 0.5 kn, 3 kn, 3.5 kn |
| `GPS_LOST_SECONDS`, `AIS_LOST_SECONDS`, `ALERT_REPEAT_SECONDS` | `link_monitor.py` | 30 s, 600 s, 600 s |
| Own ship MMSI and start position | `__main__` of each program | 244030153, Jemgum |

## Logging

`ais_stream.py` logs to `ais_stream.log` beside the script at level WARNING,
rotated at 1 MB with 3 backups. The `warn` logger is set to INFO, so generated
and issued warnings are also logged.

## Tests

```
for t in test_*.py; do python3 -m unittest ${t%.py}; done
```

The root test files cover the main AIS programs:
- `test_cpa_tracker.py`
- `test_gps_stream.py`
- `test_link_monitor.py`
- `test_multi_process_ais.py`
- `test_monitor_window.py`
- `test_register_msg.py`
- `test_ships.py`
- `test_warn.py`

Tests for optional/experimental tools are under `extra/` and can be run with
`python3 -m unittest extra.test_ais_message_window extra.test_ais_stream_threads extra.test_geo_calcule`.

`test_warn.py` imports `speech.py`, which loads the Kokoro model, so
those tests need the model files present.

## Known limitations

- CPA uses a flat-earth approximation. It is accurate at the ranges that
  matter (a few NM) but not for long distances.
- The 10-second CPA refresh runs when an AIS message arrives; on a completely
  silent stream nothing is refreshed.
- `extra/ais_stream_threads.py` has no CPA refresh between reports.
# speaking_ais
