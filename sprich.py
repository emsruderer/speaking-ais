"""Deutsche AIS-Sprachausgabe mit dem Piper-Modell Thorsten auf der CPU.

Installiere die Abhängigkeiten mit:
    pip install piper-tts sounddevice numpy

Lege de_DE-thorsten-high.onnx und die zugehörige
de_DE-thorsten-high.onnx.json neben dieses Skript.
"""

import queue
import re
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
from piper import PiperVoice, SynthesisConfig

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "de_DE-thorsten-high.onnx"
SAMPLE_RATE = 22050
SPEED = 0.7
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

if not MODEL_PATH.is_file():
    raise FileNotFoundError(f"Piper-Modell nicht gefunden: {MODEL_PATH}")

voice = PiperVoice.load(MODEL_PATH)
_SYNTHESIS_CONFIG = SynthesisConfig(length_scale=1 / SPEED)

_EINER = (
    "null",
    "eins",
    "zwei",
    "drei",
    "vier",
    "fünf",
    "sechs",
    "sieben",
    "acht",
    "neun",
    "zehn",
    "elf",
    "zwölf",
    "dreizehn",
    "vierzehn",
    "fünfzehn",
    "sechzehn",
    "siebzehn",
    "achtzehn",
    "neunzehn",
)
_ZEHNER = (
    "",
    "",
    "zwanzig",
    "dreißig",
    "vierzig",
    "fünfzig",
    "sechzig",
    "siebzig",
    "achtzig",
    "neunzig",
)
_GROSSE_ZAHLEN = (
    (1_000_000_000_000_000_000, "Trillion", "Trillionen"),
    (1_000_000_000_000_000, "Billiarde", "Billiarden"),
    (1_000_000_000_000, "Billion", "Billionen"),
    (1_000_000_000, "Milliarde", "Milliarden"),
    (1_000_000, "Million", "Millionen"),
    (1000, "tausend", "tausend"),
)

spell_alphabet = {
    "A": "Anton",
    "B": "Berta",
    "C": "Cäsar",
    "D": "Dora",
    "E": "Emil",
    "F": "Friedrich",
    "G": "Gustav",
    "H": "Heinrich",
    "I": "Ida",
    "J": "Julius",
    "K": "Kaufmann",
    "L": "Ludwig",
    "M": "Martha",
    "N": "Nordpol",
    "O": "Otto",
    "P": "Paula",
    "Q": "Quelle",
    "R": "Richard",
    "S": "Samuel",
    "T": "Theodor",
    "U": "Ulrich",
    "V": "Viktor",
    "W": "Wilhelm",
    "X": "Xanthippe",
    "Y": "Ypsilon",
    "Z": "Zacharias",
    "0": "null",
    "1": "eins",
    "2": "zwei",
    "3": "drei",
    "4": "vier",
    "5": "fünf",
    "6": "sechs",
    "7": "sieben",
    "8": "acht",
    "9": "neun",
}

nato_alphabet = {
    "A": "Alfa",
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
}


def _unter_tausend(number: int) -> str:
    if number < 20:
        return _EINER[number]
    if number < 100:
        zehner, einer = divmod(number, 10)
        if einer:
            return f"{_EINER[einer][:-1] if einer == 1 else _EINER[einer]}und{_ZEHNER[zehner]}"
        return _ZEHNER[zehner]
    hunderter, rest = divmod(number, 100)
    prefix = "einhundert" if hunderter == 1 else f"{_EINER[hunderter]}hundert"
    return prefix + (_unter_tausend(rest) if rest else "")


def str_getal(number: int) -> str:
    """Wandle eine nicht negative Ganzzahl in deutsche Wörter um."""
    number = abs(int(number))
    if number < 1000:
        return _unter_tausend(number)

    for scale, singular, plural in _GROSSE_ZAHLEN:
        if number >= scale:
            count, remainder = divmod(number, scale)
            if scale == 1000:
                result = ("ein" if count == 1 else str_getal(count)) + singular
            else:
                count_words = "eine" if count == 1 else str_getal(count)
                result = f"{count_words} {singular if count == 1 else plural}"
            return result + (f" {str_getal(remainder)}" if remainder else "")

    raise ValueError(f"Zahl kann nicht ausgesprochen werden: {number}")


def str_number(number: float) -> str:
    """Wandle eine ganze oder dezimale Zahl in deutsche Wörter um."""
    vorzeichen = "minus " if number < 0 else ""
    zahltext = str(abs(number))
    if isinstance(number, float) and number.is_integer():
        zahltext = str(int(abs(number)))
    ganzzahl, komma, nachkomma = zahltext.partition(".")
    ergebnis = str_getal(int(ganzzahl))
    if komma:
        ergebnis += " Komma " + " ".join(_EINER[int(ziffer)] for ziffer in nachkomma)
    return vorzeichen + ergebnis


def spell_callsign(callsign: str, alphabet: str = "de") -> str:
    """Buchstabiere ein Rufzeichen deutsch oder mit dem NATO-Alphabet.

    Zahlen werden in beiden Varianten auf Deutsch gesprochen.
    """
    if alphabet == "de":
        code = spell_alphabet
    elif alphabet == "nato":
        code = spell_alphabet | nato_alphabet
    else:
        raise ValueError("alphabet muss 'de' oder 'nato' sein")
    try:
        return " ".join(code[zeichen] for zeichen in callsign.upper())
    except KeyError as error:
        raise ValueError(f"Nicht unterstütztes Zeichen im Rufzeichen: {error.args[0]!r}") from error


class _Player:
    """Synthesisiert Nachrichten im Hintergrund und spielt sie ohne Dateizwischenablage ab."""

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

    def _fill(self, outdata, frames, _time_info, _status) -> None:
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
                    for chunk in voice.synthesize(
                        sentence, syn_config=_SYNTHESIS_CONFIG
                    ):
                        if (
                            chunk.sample_rate != SAMPLE_RATE
                            or chunk.sample_width != 2
                            or chunk.sample_channels != 1
                        ):
                            raise RuntimeError(
                                "Unerwartetes Piper-Audioformat: "
                                f"{chunk.sample_rate} Hz, {chunk.sample_width} Byte, "
                                f"{chunk.sample_channels} Kanal/Kanäle"
                            )
                        samples = (
                            np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
                            .astype(np.float32)
                            / 32768.0
                        )
                        self._chunks.put((samples, None))
            except Exception as error:  # wird an den Aufrufer von speak() weitergegeben
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


def speak_tts(text: str) -> None:
    """Sprich einen deutschen Text und kehre nach der Wiedergabe zurück."""
    _player.say(text)


def say_number(number: float) -> None:
    """Sprich eine Zahl auf Deutsch."""
    speak_tts(str_number(number))


def speak(message: str) -> None:
    """Sprich eine Nachricht, ohne eine Audiodatei anzulegen."""
    speak_tts(message)


sprich = speak


if __name__ == "__main__":
    nachricht = (
        "Ein niederländisches Schiff nähert sich mit null Komma drei Knoten, "
        "Kurs dreihundertfünfunddreißig Grad, Rufzeichen "
        f"{spell_callsign('PF2947')}. Das Schiff heißt Laverna und ist ein "
        "Frachtschiff. Der nächste Annäherungspunkt wird in sieben Minuten "
        "erreicht und liegt dreihundertsechsundachtzig Meter entfernt."
    )

    print(spell_callsign("PC1234"))
    speak("MMSI " + spell_callsign("264030000", alphabet="nato"))
    speak(spell_callsign("PC1234", alphabet="nato"))
    speak(str_number(0.5))
    speak(str_number(-3))
    speak(nachricht)
    speak("Das AIS-Sprachwarnsystem ist gestartet.")
    speak("Schiffsname: Johanna.")
    speak("Rufzeichen " + spell_callsign("PC1234", alphabet="nato"))
