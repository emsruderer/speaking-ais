""" multi_process_ais: read, track and warn about AIS traffic in separate processes

    reader   - receives AIS from SignalK and queues the raw (assembled) messages
    decoder  - decodes them, keeps vessels within settings.AIS_RANGE_NM of the own
               ship, tracks CPA/TCPA and queues dangerous tracks
    warning  - speaks the warnings (warn.do_warn)
"""
import argparse
import logging
import multiprocessing
import signal
import time
from queue import Empty, Full

import settings
from ais_stream import (
    add_mode_argument,
    apply_mode_argument,
    open_stream,
    position_in_range,
    start_warning_process,
    supervise_warning_process,
    track_messages,
)
from cpa_tracker import available
from link_monitor import LinkMonitor
from queue_metrics import increment
from ships import Ship

HOST = settings.HOST
PORT = 10110
MAX_MESSAGES = 100
SUPERVISE_SECONDS = 5  # how often a dead warning process is detected and restarted

JEMGUM_LAT = 53.26379
JEMGUM_LON = 7.39738
JOHANNA = 244030153

POSITION_TYPES = (1, 2, 3, 18)
STATIC_TYPES = (5, 19, 24)

log = logging.getLogger(__name__)


def show_msg(msg):
    """pretty print """
    for k,v in msg.items():
        print(k,v)
    print('_' * 20)


def do_read(raw_que, host=HOST, port=PORT, raw_enqueued=None):
    """Reader process: queue every assembled AIS message, reconnecting on errors."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        try:
            with open_stream(host, port) as stream:
                for raw in stream:
                    try:
                        raw_que.put_nowait(raw)
                        increment(raw_enqueued)
                    except Full:
                        log.warning("Decoder is behind; dropped AIS message")
            log.warning("AIS stream %s:%s closed; reconnecting in 5 seconds", host, port)
        except OSError as error:
            log.error("AIS connection to %s:%s failed: %s; retrying in 5 seconds",
                      host, port, error)
        time.sleep(5)


class Decoder:
    """Decode queued AIS messages and track the vessels around the own ship."""

    def __init__(self, q, wq, owner_ship, stop_event, raw_dequeued=None,
                 warning_enqueued=None, ais_seen=None):
        self.q = q
        self.warn_que = wq
        self.ship = owner_ship
        self.stop_event = stop_event
        self.raw_dequeued = raw_dequeued
        self.warning_enqueued = warning_enqueued
        self.ais_seen = ais_seen

    def messages(self):
        """Yield decoded messages of interest until the stop event is set."""
        while not self.stop_event.is_set():
            try:
                raw = self.q.get(timeout=1)
            except Empty:
                continue
            increment(self.raw_dequeued)
            if self.ais_seen is not None:
                self.ais_seen()
            try:
                msg = raw.decode()
            except Exception as error:  # a corrupt message must not stop decoding
                log.warning("Skipping undecodable AIS message: %s", error)
                continue
            if msg.mmsi is None or msg.msg_type not in POSITION_TYPES + STATIC_TYPES:
                continue
            if msg.msg_type in POSITION_TYPES:
                lat, lon = available("lat", msg.lat), available("lon", msg.lon)
                if lat is None or lon is None:
                    continue
                if not position_in_range(lat, lon, *self.ship.get_position()):
                    continue
                if settings.VERBOSE:
                    show_msg(msg.asdict())
            yield msg

    def do_decode(self):
        """Decode Process"""
        track_messages(
            self.messages(), self.warn_que, self.ship, self.stop_event,
            warning_enqueued=self.warning_enqueued,
        )


def run_decoder(raw_que, warn_que, stop_event, host=HOST, port=PORT,
                raw_dequeued=None, warning_enqueued=None):
    """Decoder process; the own-ship thread must run in the process that uses it."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    my_ship = Ship(JOHANNA, JEMGUM_LAT, JEMGUM_LON, 0.0, 0.0, host, port)
    my_ship.start()
    link_monitor = LinkMonitor(my_ship, warn_que, warning_enqueued)
    link_monitor.start()
    try:
        Decoder(
            raw_que, warn_que, my_ship, stop_event, raw_dequeued,
            warning_enqueued, link_monitor.ais_seen,
        ).do_decode()
    finally:
        link_monitor.stop()
        my_ship.stop()


def stop_warning_process(process, que):
    """Ask the warning process to finish, terminating it if it does not."""
    try:
        que.put(None, timeout=5)
        process.join(timeout=10)
    except Full:
        log.warning("Warning queue remained full during shutdown")
    if process.is_alive():
        process.terminate()
        process.join()

def main(monitor=False):
    """Main entry point; the Tk queue monitor is shown only when monitor is True."""
    # Initialize logging
    logging.basicConfig(level=logging.ERROR)
    raw_que = multiprocessing.Queue(MAX_MESSAGES)
    warn_que = multiprocessing.Queue(MAX_MESSAGES)
    stop_event = multiprocessing.Event()
    raw_enqueued = multiprocessing.Value("L", 0)
    raw_dequeued = multiprocessing.Value("L", 0)
    warning_enqueued = multiprocessing.Value("L", 0)
    warning_dequeued = multiprocessing.Value("L", 0)
    reader = multiprocessing.Process(target=do_read,
                                     args=(raw_que, HOST, PORT, raw_enqueued),
                                     name="ais-reader", daemon=True)
    decoder = multiprocessing.Process(target=run_decoder,
                                      args=(raw_que, warn_que, stop_event, HOST,
                                            PORT, raw_dequeued, warning_enqueued),
                                      name="ais-decoder")
    warning = start_warning_process(warn_que, warning_dequeued)
    for process in (decoder, reader):
        process.start()
    try:
        if monitor:
            import tkinter as tk
            from monitor_window import QueueMonitorWindow
            root = tk.Tk()
            window = QueueMonitorWindow(
                root,
                (
                    ("Raw AIS messages (reader → decoder)", raw_que, MAX_MESSAGES,
                     raw_enqueued, raw_dequeued),
                    ("Danger warnings (decoder → warning)", warn_que, MAX_MESSAGES,
                     warning_enqueued, warning_dequeued),
                ),
                processes=(("Reader", reader), ("Decoder", decoder),
                           ("Warning", warning)),
            )

            def supervise():
                nonlocal warning
                warning = supervise_warning_process(warning, warn_que,
                                                    warning_dequeued)
                window.processes = (("Reader", reader), ("Decoder", decoder),
                                    ("Warning", warning))
                root.after(SUPERVISE_SECONDS * 1000, supervise)

            root.after(SUPERVISE_SECONDS * 1000, supervise)
            root.mainloop()
        else:
            while decoder.is_alive():
                decoder.join(timeout=SUPERVISE_SECONDS)
                warning = supervise_warning_process(warning, warn_que,
                                                    warning_dequeued)
            log.error("Decoder process exited with code %s", decoder.exitcode)
    except KeyboardInterrupt:
        log.info("Shutdown requested")
    finally:
        stop_event.set()
        decoder.join(timeout=10)
        if decoder.is_alive():
            decoder.terminate()
            decoder.join()
        # The reader blocks in a socket read; nothing uses raw_que after this.
        reader.terminate()
        reader.join()
        stop_warning_process(warning, warn_que)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--monitor", action="store_true",
                        help="show the queue monitor window (default: no monitor)")
    add_mode_argument(parser)
    cli_args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    apply_mode_argument(cli_args)  # set before the worker processes are forked
    main(monitor=cli_args.monitor)
