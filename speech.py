"""English AIS speech using the Kokoro v1.0 ONNX model on CPU.

Install the Python dependencies with:
    pip install kokoro-onnx sounddevice

Keep kokoro-v1.0.onnx and voices-v1.0.bin beside this script. On a Raspberry Pi,
also put kokoro-v1.0.int8.onnx there; it is used when present and is several
times smaller and faster than the full-precision model.
"""

import os
import queue
import re
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sounddevice as sd
from kokoro_onnx import Kokoro

BASE_DIR = Path(__file__).resolve().parent
INT8_MODEL_PATH = BASE_DIR / "kokoro-v1.0.int8.onnx"
MODEL_PATH = (INT8_MODEL_PATH if INT8_MODEL_PATH.is_file()
              else BASE_DIR / "kokoro-v1.0.onnx")
VOICES_PATH = BASE_DIR / "voices-v1.0.bin"

print(f"Using model: {MODEL_PATH}")
print(f"Using voices: {VOICES_PATH}")

VOICE = "af_heart"
LANGUAGE = "en-us"
SPEED = 0.7
SAMPLE_RATE = 24000  # Kokoro output rate
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

for asset_path in (MODEL_PATH, VOICES_PATH):
    if not asset_path.is_file():
        raise FileNotFoundError(f"Kokoro asset not found: {asset_path}")

if "CPUExecutionProvider" not in ort.get_available_providers():
    raise RuntimeError("ONNX Runtime does not provide CPUExecutionProvider")

session_options = ort.SessionOptions()
session_options.intra_op_num_threads = os.cpu_count() or 4  # all Pi 4 cores
session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
session = ort.InferenceSession(
    str(MODEL_PATH),
    sess_options=session_options,
    providers=["CPUExecutionProvider"],
)
tts = Kokoro.from_session(session, str(VOICES_PATH))

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)

spell_alphabet = {
    "A": "Alpha",
    "B": "Bravo",
    "C": "Charlie",
    "D": "Delta",
    "E": "Echo",
    "F": "Foxtrot",
    "G": "Golf",
    "H": "Hotel",
    "I": "India",
    "J": "Juliett",
    "K": "Kilo",
    "L": "Lima",
    "M": "Mike",
    "N": "November",
    "O": "Oscar",
    "P": "Papa",
    "Q": "Quebec",
    "R": "Romeo",
    "S": "Sierra",
    "T": "Tango",
    "U": "Uniform",
    "V": "Victor",
    "W": "Whiskey",
    "X": "X-ray",
    "Y": "Yankee",
    "Z": "Zulu",
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}


def _under_thousand(number: int) -> str:
    if number < 20:
        return _ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")
    hundreds, remainder = divmod(number, 100)
    result = f"{_ONES[hundreds]} hundred"
    return result + (f" {_under_thousand(remainder)}" if remainder else "")


def str_getal(number: int) -> str:
    """Convert a non-negative integer to English words."""
    number = abs(int(number))
    if number < 1000:
        return _under_thousand(number)

    for scale, name in (
        (1_000_000_000, "billion"),
        (1_000_000, "million"),
        (1000, "thousand"),
    ):
        if number >= scale:
            count, remainder = divmod(number, scale)
            result = f"{str_getal(count)} {name}"
            return result + (f" {str_getal(remainder)}" if remainder else "")

    raise ValueError(f"Number is too large to spell: {number}")


def str_number(number: float) -> str:
    """Convert an integer or decimal number to English words."""
    sign = "minus " if number < 0 else ""
    number_text = str(abs(number))
    if isinstance(number, float) and number.is_integer():
        number_text = str(int(abs(number)))
    whole, decimal, fraction = number_text.partition(".")
    result = str_getal(int(whole))
    if decimal:
        result += " point " + " ".join(_ONES[int(digit)] for digit in fraction)
    return sign + result


def spell_callsign(callsign: str) -> str:
    """Spell an alphanumeric callsign using the NATO phonetic alphabet."""
    try:
        return " ".join(spell_alphabet[character] for character in callsign.upper())
    except KeyError as error:
        raise ValueError(f"Unsupported callsign character: {error.args[0]!r}") from error


class _Player:
    """Keeps one output stream open and synthesizes sentences in a thread.

    The stream plays silence between messages, so the audio device never goes
    to sleep and clips the start of the next message. Playback of the first
    sentence starts while later sentences are still being synthesized."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._jobs: queue.Queue = queue.Queue()
        self._chunks: queue.Queue = queue.Queue()
        self._current = None
        self._position = 0
        self._stream = None

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self._fill,
            )
            self._stream.start()
            threading.Thread(target=self._synthesize, daemon=True).start()
            self._started = True

    def _fill(self, outdata, frames, time_info, status) -> None:
        outdata.fill(0)
        filled = 0
        while filled < frames:
            if self._current is None:
                try:
                    self._current = self._chunks.get_nowait()
                except queue.Empty:
                    return
                self._position = 0
            samples, done = self._current
            count = min(frames - filled, len(samples) - self._position)
            outdata[filled:filled + count, 0] = (
                samples[self._position:self._position + count])
            filled += count
            self._position += count
            if self._position >= len(samples):
                if done is not None:
                    done.set()
                self._current = None

    def _synthesize(self) -> None:
        while True:
            text, done, errors = self._jobs.get()
            try:
                for sentence in _SENTENCE_END.split(text.strip()):
                    if not sentence.strip():
                        continue
                    samples, rate = tts.create(
                        sentence,
                        voice=VOICE,
                        speed=SPEED,
                        lang=LANGUAGE,
                        trim=False,
                    )
                    if rate != SAMPLE_RATE:
                        raise RuntimeError(f"Unexpected sample rate {rate}")
                    self._chunks.put((samples.astype(np.float32), None))
            except Exception as error:  # reported to the caller of speak()
                errors.append(error)
            self._chunks.put((np.zeros(0, dtype=np.float32), done))

    def say(self, text: str) -> None:
        self._ensure_started()
        done = threading.Event()
        errors: list = []
        self._jobs.put((text, done, errors))
        done.wait()
        if errors:
            raise errors[0]


_player = _Player()


def speak_tts(txt: str) -> None:
    """Synthesize English speech and play it, returning when it has finished."""
    _player.say(txt)


def say_number(number: float) -> None:
    """Speak a number in English."""
    speak_tts(str_number(number))


def speak(msg: str) -> None:
    """Synthesize and play a message without creating a file."""
    speak_tts(msg)


if __name__ == "__main__":
    message = (
        "An approaching dutch vessel is making zero point three knots, on course "
        "three hundred thirty-five degrees, with call sign "
        f"{spell_callsign('PF2947')}. The ship is named Laverna and is a cargo "
        "vessel. Closest point of approach in seven minutes, at a distance of "
        "three hundred eighty-six meters. Currently four nautical miles away, bearing "
        "one hundred twenty degrees."
    )

    print(spell_callsign("PC1234"))
    speak("MMSI " + spell_callsign(str(264030000)))  
    speak(spell_callsign("PC1234"))
    speak(str_number(0.5))
    speak(str_number(-3))
    speak(message)
    speak("AIS voice announcement system started.")
    speak("Ship name: Johanna.")
    speak("Call sign " + spell_callsign("PC1234"))

    for number in (
        *range(9, 13),
        *range(19, 22),
        *range(199, 203),
        *range(999, 1003),
        *range(2995, 3005),
    ):
        speak(str_number(number))
