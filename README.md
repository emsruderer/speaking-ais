# AIS stream collision watcher

`ais_stream.py` is the main AIS tracking and collision-warning program. It
connects to a SignalK NMEA 0183 TCP stream, tracks nearby AIS vessels, calculates
closest point of approach (CPA) and time to CPA (TCPA) relative to the own ship,
and speaks warnings about dangerous encounters.

The program reads the own ship's GPS navigation data from the same SignalK
stream. It does not open a graphical interface; see
[NMEA 0183 monitor window](#nmea-0183-monitor-window) for the separate monitor.

## Run

```bash
python3 ais_stream.py [--open | --river] [--speech-language en | de]
```

- `--river` selects river/channel warning thresholds; this is the default.
- `--open` selects wider open-water thresholds.
- `--speech-language en` uses English Kokoro speech; `de` uses German Piper
  speech. English is the default.
- The SignalK host and port are set in `settings.py`: `HOST = "192.168.0.100"`
  and `PORT = 10110`. There are no command-line host or port options.

The own ship's MMSI and start position are set in `settings.py` (`OWN_MMSI`,
`OWN_START_LAT`, `OWN_START_LON`); it starts with zero course and speed. The live GPS stream updates that state after startup.

## Requirements

- Python 3.12
- AIS decoding uses the vendored `pyais/` package.
- The GPS/NMEA decoder and tracking calculations use project modules and the
  Python standard library.
- Audio output requires a working audio device and:
  - English: `kokoro-onnx`, `onnxruntime`, `numpy`, and `sounddevice`, with
    `voices-v1.0.bin` and either `kokoro-v1.0.int8.onnx` or
    `kokoro-v1.0.onnx` beside `speech.py`. The int8 model is used when present.
  - German: `piper-tts`, `numpy`, and `sounddevice`, with
    `de_DE-thorsten-high.onnx` and its `.onnx.json` config beside `sprich.py`.

Speech is loaded by the warning worker. Install only the dependencies and model
files for the selected language.

## How it works

The program runs the own-ship GPS reader in a thread, reads AIS in the main
process, and handles spoken warnings in a separate worker process. A link
monitor watches for lost or restored GPS and AIS data and puts system alerts on
the same warning queue.

The `Ship` reader uses valid RMC, GLL, VTG, ZDA, and HDT navigation sentences.
When no valid GPS fix has arrived for 10 seconds, or the GPS stream fails, it
uses dead reckoning from its last position, course, and speed. The link monitor
speaks a GPS-loss alert after 30 seconds without a fix and an AIS-loss alert
after 600 seconds without an AIS sentence.

The AIS stream accepts message types 1, 2, 3, 5, 18, 19, and 24:

- Position reports (1, 2, 3, 18) update the vessel's position and motion.
- Static reports (5, 19, 24) add vessel identity and dimensions.
- Invalid or unavailable AIS positions are ignored.
- Tracks are recalculated against the latest own-ship state every five seconds
  while AIS messages are being processed. Targets are projected forward from
  their last report.

CPA is calculated on a local flat-earth projection, in nautical miles. TCPA is
in minutes; a positive value is time until closest approach. Warning CPA limits
use estimated hull-to-hull clearance, accounting for known AIS vessel
dimensions and the configured own-ship beam.

### Warning thresholds

All profiles require a positive TCPA below the profile's time limit and a
clearance below its CPA limit. A warning is considered when the current range
is below the profile's distance limit, or when TCPA is below its
`always_warn_tcpa` limit.

| Profile | CPA clearance | TCPA | Range | Early warning | Additional filtering |
|---|---:|---:|---:|---:|---|
| River (default) | 50 m | 10 min | 0.5 NM | Disabled | Average 5 AIS velocity reports; require 2 consecutive dangerous reports |
| Open | 1 NM | 30 min | 5 NM | 10 min | No smoothing or report confirmation |

Slow Class B targets (types 18, 19, and 24, at 7 knots or less) use a tighter
CPA limit: 25 m and 5 minutes in river mode, or 0.3 NM and 10 minutes in open
mode.

To avoid warnings caused by noisy velocity estimates, the program also
suppresses a warning if range has not closed by at least 20 m over a 45-second
observation window. The warning queue holds up to 100 items; if it is full, a
new warning is dropped and logged rather than blocking AIS tracking.

### Optional manoeuvring mode

Set `QUIET_MANOEUVRING = True` in `settings.py` to enable special handling while
the own ship is manoeuvring. Manoeuvring starts below 3 knots and ends at
3.5 knots. Stationary targets are ignored during this period; moving targets
under 3 knots use close-range harbour thresholds, while faster targets keep the
selected base profile.

### Dead reckoning when GPS is lost

Ten seconds after the last fix, `ships.py` extrapolates the own position. Two
tracks run side by side:

- **Course and speed**: the last GPS course and speed over ground are kept.
- **Sensor model** (`dead_reckoning.py`): true heading (HDT, or HDG/HDM plus
  variation), speed through water (VHW) and leeway from heel (XDR `ROLL`/`HEEL`),
  corrected by what was learned while GPS was present: the tidal current (mean
  over the last 3 minutes), the log scale factor and the compass offset (these
  two need course changes to be learned). Without speed through water the last
  GPS ground vector is turned with the heading; without heading the model falls
  back to course and speed.

With `DEAD_RECKONING_SENSORS = False` (default) warnings use the course-and-speed
track and the model runs in shadow. After every GPS outage longer than 30 s, the
log records the position error of both tracks and the learned values, e.g.
`GPS back after 312 s; dead-reckoning error: 640 m keeping course and speed,
35 m with the sensor model (water+current; log scale 0.981, heading offset
+2.1 deg, current 205 deg 0.84 kn)`. Set `DEAD_RECKONING_SENSORS = True` once
these reports show the model is reliable on board. `MAGNETIC_VARIATION` and
`LEEWAY_COEFFICIENT` in `settings.py` are described there.

## NMEA 0183 monitor window

`nmea_window.py` is a separate graphical monitor for the same SignalK NMEA 0183
stream. It shows one field for each sentence identifier, such as `$GLGSV`,
`$GNRMC`, `$GNGSA` or `!AIVDM`, and adds a GNSS jamming warning.

```bash
python3 nmea_window.py [--host HOST] [--port PORT] [--columns N]
```

`--host` and `--port` default to `HOST` and `PORT` in `settings.py`;
`--columns` defaults to 2. The window needs Tkinter and a desktop session.

### Sentence fields

A field is created the first time its sentence arrives. Each field shows the
decoded values, the number of sentences received, the time since the last one,
and the raw sentence. The field title turns grey after 5 seconds without data.

- GSV (`$GPGSV`, `$GLGSV`, `$GAGSV`, `$GBGSV`, ...): the multipart cycle is
  assembled per signal before display. The table lists PRN, elevation,
  azimuth with compass direction, SNR with a bar and a quality rating, and a ✓
  when the satellite is used in the fix according to GSA. NMEA 4.10/4.11
  signal IDs are named per system, for example GLONASS `1` = G1 C/A, Galileo
  `7` = E1-BC and BeiDou `5` = B2a. GLONASS rows also show the slot number
  (PRN 65–96 is slot 1–32). Satellites below 10° are marked as multipath
  risks; satellites above 60° with less than 25 dB are marked as possibly
  shadowed.
- GSA: fix mode, fix type, PDOP/HDOP/VDOP and the satellites used, per system.
- RMC, GGA, GLL, VTG, ZDA, HDT: decoded values, with the position in decimal
  degrees and degrees/minutes.
- HDM (e.g. pypilot `$APHDM`): magnetic heading.
- HDG (e.g. `$IIHDG`): sensor heading, deviation and variation (east
  positive), the magnetic heading (sensor + deviation) and, when the
  variation is given, the true heading (magnetic + variation).
- XDR (e.g. pypilot `$APXDR`): one line per transducer, with name, value,
  unit and type, for example `pitch (PTCH)  -0.595°  angle`. pypilot sends
  pitch and roll in separate sentences; the field keeps the latest value of
  each.
- VDM/VDO: the most recent AIS message, decoded.
- `$PAIRSPF` / `$PAIRSPF5`: the receiver's own L1 / L5 jamming status (see
  below); `$PAIR001`: acknowledgement of a receiver command.
- Other sentences, including other proprietary `$P...` sentences: numbered raw
  fields.

The structured decoding is in `gps_stream.py`: `parse_gsv`, `analyze_gsv`,
`decode_gsv_cycle`, `parse_gsa`, `decode_gga` and the stateful
`NmeaFieldDecoder`. `decode_line` and `gps_from_signalk` are unchanged and do
not return GSV, GSA or GGA data.

### Jamming warning

`JammingMonitor` in `gps_stream.py` estimates interference from GSV signal
strength and fix status. The window shows its level in a coloured banner, a
table per frequency band and the last four level changes. On a rise to warning
or alarm the window rings the bell, prefixes its title with ⚠ and writes a
timestamped line to stderr.

- During the first 30 GSV cycles of each satellite signal the monitor learns a
  baseline SNR (level `learning`). After that the baseline follows slowly.
  Only signals with a baseline of at least 15 dB are monitored, and a band
  needs at least 4 of them.
- Signals are grouped by band (L1, L2, L5, E6, S). Signals without a signal ID
  count as L1.
- Each band compares the current SNR of every satellite with its baseline. A
  satellite that is no longer tracked counts as a full drop.

| Level | Median SNR drop | Satellites dropped ≥ 6 dB |
|---|---:|---:|
| Watch | ≥ 3 dB | ≥ 50 % |
| Warning | ≥ 6 dB | ≥ 60 % |
| Alarm | ≥ 10 dB | ≥ 70 % |

- Losing the position fix (RMC status `V`, GGA quality 0, or GSA no fix) for
  2 seconds during a warning raises it to alarm.
- An L1 drop while another band such as L5 stays normal is reported as
  L1-band interference, typical of a GPS jammer. A drop on all bands is
  reported as jamming, an antenna or cable fault, or an obstruction.
- Obstructions that affect only part of the sky do not reach the warning level.
- A higher level is shown after it has lasted 3 seconds; a lower level after
  10 seconds. While a warning clears, the banner says "Signals recovering".
- While a band is at watch level or higher, its baselines are frozen, so a
  continuous jammer does not become the new normal. After moving the antenna,
  press **Re-learn baseline**; otherwise lower signal levels at the new place
  can be reported as interference.

The monitor does not detect spoofing.

### Receiver jamming detection (Quectel LC29H)

The GPS is a Waveshare LC29H(AA) HAT on the CP2102N USB serial port, read by
SignalK at 115200 baud. The LC29H has its own jamming detector, which is off
by default. `$PAIR391,1` switches it on; the receiver then sends `$PAIRSPF`
(L1) and `$PAIRSPF5` (L5) once per second with status 0 = unknown,
1 = no jamming, 2 = warning, 3 = critical. It does not output raw AGC or
noise values.

```bash
python3 lc29h_jamming.py --enable --save   # switch on and save to flash
python3 lc29h_jamming.py --status          # show the current $PAIRSPF status
python3 lc29h_jamming.py --disable --save  # switch off
```

`--save` sends `$PAIR513`, which stores the setting in flash; without it the
setting is lost when the receiver loses power. The script writes to the GPS
serial port and reads the `$PAIR001` acknowledgement from SignalK's TCP
stream. If SignalK locks the port, stop SignalK while running the script:
`sudo systemctl stop signalk`, run the command, then
`sudo systemctl start signalk`.

When `$PAIRSPF` sentences arrive, `JammingMonitor` uses them as a second
source: receiver status 2 raises the level to at least warning, status 3 to
alarm, with the same 3-second onset and 10-second clear delay. A status older
than 5 seconds is ignored. The jamming panel shows the receiver status per
band, or "not reported" when no `$PAIRSPF` is received.

## Configuration

| Setting | Default | Purpose |
|---|---:|---|
| `HOST` | `"192.168.0.100"` | SignalK NMEA TCP server |
| `PORT` | `10110` | SignalK NMEA TCP port |
| `NAVIGATION_MODE` | `"river"` | Base warning profile: `"river"` or `"open"` |
| `QUIET_MANOEUVRING` | `False` | Enable manoeuvring-specific target filtering |
| `OWN_BEAM_M` | `3.5` | Own-ship beam used for hull clearance |
| `OWN_ANTENNA_OFFSET_M` | `0.0` | GPS antenna offset from the own-ship centerline |
| `SET_SYSTEM_CLOCK` | `True` | Allow a valid GPS RMC sentence to set system time |
| `VERBOSE` | `False` | Print recent tracks while processing |

The stream read timeout is 60 seconds; connection failures and closed streams
are retried after five seconds.

## Logs and tests

Warnings and errors are written to `ais_stream.log` beside `ais_stream.py`, with
rotation after 1 MB and up to three backup files.

Run the tests for the `ais_stream.py` runtime path with:

```bash
python3 -m unittest test_cpa_tracker test_gps_stream test_link_monitor \
  test_own_velocity test_range_watcher test_register_msg test_ships test_warn
```

`test_warn` loads the English speech module and therefore requires its Kokoro
dependencies and model assets.
