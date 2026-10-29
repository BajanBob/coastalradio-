"""Station identification announcements: position formats, Maidenhead grid square,
phonetic call sign, recorded announcement library and playback to the radio's audio input.

Playback is always started by the operator from the dashboard. Keying the
transmitter (PTT) is done by the operator or by the radio's own VOX / data
interface; Ocean City Watch never keys a radio itself.
"""

from __future__ import annotations

import re
import threading
import time
import wave
from pathlib import Path

PHONETIC = {
    "A": "ALFA", "B": "BRAVO", "C": "CHARLIE", "D": "DELTA", "E": "ECHO", "F": "FOXTROT", "G": "GOLF",
    "H": "HOTEL", "I": "INDIA", "J": "JULIETT", "K": "KILO", "L": "LIMA", "M": "MIKE", "N": "NOVEMBER",
    "O": "OSCAR", "P": "PAPA", "Q": "QUEBEC", "R": "ROMEO", "S": "SIERRA", "T": "TANGO", "U": "UNIFORM",
    "V": "VICTOR", "W": "WHISKEY", "X": "X-RAY", "Y": "YANKEE", "Z": "ZULU", "0": "ZERO", "1": "ONE",
    "2": "TWO", "3": "THREE", "4": "FOUR", "5": "FIVE", "6": "SIX", "7": "SEVEN", "8": "EIGHT", "9": "NINER",
}


def phonetic(s: str) -> str:
    return " ".join(PHONETIC.get(c, c) for c in s.upper() if not c.isspace())


def maidenhead(lat: float, lon: float, chars: int = 6) -> str:
    lon, lat = lon + 180, lat + 90
    out = chr(65 + int(lon // 20)) + chr(65 + int(lat // 10))
    lon, lat = lon % 20, lat % 10
    out += str(int(lon // 2)) + str(int(lat // 1))
    if chars >= 6:
        lon, lat = lon % 2, lat % 1
        out += chr(97 + int(lon * 12)) + chr(97 + int(lat * 24))
    return out[:2] + out[2:4] + out[4:].lower() if chars >= 6 else out


def dm(lat: float, lon: float) -> tuple[str, str]:
    """Degrees and decimal minutes, the usual marine format."""
    def fmt(v, pos, neg, width):
        h = pos if v >= 0 else neg
        v = abs(v)
        d = int(v)
        m = (v - d) * 60
        return f"{d:0{width}d}°{m:06.3f}'{h}"
    return fmt(lat, "N", "S", 2), fmt(lon, "E", "W", 3)


def spoken_position(lat: float, lon: float) -> str:
    def say(v, pos, neg, width):
        h = "NORTH" if v >= 0 and pos == "N" else "SOUTH" if pos == "N" else "EAST" if v >= 0 else "WEST"
        v = abs(v)
        d = int(v)
        m = (v - d) * 60
        return f"{d:0{width}d} DEGREES {m:05.2f} MINUTES {h}"
    return f"{say(lat, 'N', 'S', 2)}, {say(lon, 'E', 'W', 3)}"


DEFAULT_TEMPLATE = (
    "THIS IS {name}, {name}, {name}. CALL SIGN {callsign_phonetic}. "
    "COAST STATION POSITION {position_spoken}. GRID SQUARE {grid_phonetic}. "
    "{name} KEEPS WATCH ON VHF CHANNEL 16. {extra} {name} OUT."
)


def script(station: dict, extra: str = "") -> dict:
    lat, lon = station.get("lat"), station.get("lon")
    if lat is None or lon is None:
        return {"error": "Set station lat/lon in config.json"}
    la, lo = dm(lat, lon)
    grid = maidenhead(lat, lon)
    fields = {
        "name": station.get("name", "COAST STATION").upper(),
        "callsign": station.get("callsign", ""),
        "callsign_phonetic": phonetic(station.get("callsign", "")),
        "lat_dm": la, "lon_dm": lo, "grid": grid, "grid_phonetic": phonetic(grid),
        "position_spoken": spoken_position(lat, lon),
        "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
        "extra": (extra or station.get("announcement_extra", "")).strip(),
    }
    tmpl = station.get("announcement_template") or DEFAULT_TEMPLATE
    try:
        text = tmpl.format(**fields)
    except (KeyError, IndexError):
        text = DEFAULT_TEMPLATE.format(**fields)
    return {"text": re.sub(r"\s+", " ", text).strip(), "lat_dm": la, "lon_dm": lo, "grid": grid,
            "lat": lat, "lon": lon}


class Announcer:
    def __init__(self, folder: Path, output_device=None, reminders: list[str] | None = None) -> None:
        self.dir = folder
        self.dir.mkdir(parents=True, exist_ok=True)
        self.output_device = output_device
        self.reminders = reminders or []
        self.playing: str | None = None
        self.last_played: str | None = None
        self._done_today: set[str] = set()
        self._stop = threading.Event()

    @staticmethod
    def safe_name(name: str) -> str:
        n = re.sub(r"[^A-Za-z0-9 _.-]", "", name).strip().replace(" ", "_")[:60] or "announcement"
        return n if n.lower().endswith(".wav") else n + ".wav"

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.wav"), key=lambda p: -p.stat().st_mtime):
            try:
                with wave.open(str(p)) as w:
                    dur = w.getnframes() / w.getframerate()
            except (wave.Error, OSError, ZeroDivisionError):
                dur = None
            out.append({"name": p.name, "seconds": round(dur, 1) if dur else None, "modified": p.stat().st_mtime})
        return out

    def save(self, name: str, data: bytes) -> str:
        if len(data) > 25_000_000 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("Upload must be a WAV file under 25 MB")
        p = self.dir / self.safe_name(name)
        p.write_bytes(data)
        try:
            with wave.open(str(p)):
                pass
        except wave.Error as e:
            p.unlink(missing_ok=True)
            raise ValueError(f"Not a readable WAV file: {e}") from e
        return p.name

    def delete(self, name: str) -> None:
        p = self.dir / self.safe_name(name)
        if p.parent == self.dir:
            p.unlink(missing_ok=True)

    def tts(self, text: str, name: str) -> str:
        try:
            import pyttsx3
        except ImportError as e:
            raise RuntimeError("Text-to-speech needs: pip install pyttsx3 (or record your own voice)") from e
        p = self.dir / self.safe_name(name)
        eng = pyttsx3.init()
        eng.setProperty("rate", 150)
        eng.save_to_file(text, str(p))
        eng.runAndWait()
        return p.name

    def play(self, name: str) -> None:
        """Operator-initiated playback to the configured output (the radio's audio/mic input)."""
        if self.playing:
            raise RuntimeError("Already playing")
        p = self.dir / self.safe_name(name)
        if not p.is_file():
            raise FileNotFoundError(name)
        import numpy as np
        import sounddevice as sd
        with wave.open(str(p)) as w:
            fs, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[sw]
        data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if sw == 1:
            data = (data - 128) / 128
        else:
            data /= float(np.iinfo(dtype).max)
        data = data.reshape(-1, ch)
        self.playing = p.name
        self._stop.clear()

        def run():
            try:
                sd.play(data, fs, device=self.output_device)
                end = time.time() + len(data) / fs + 0.3
                while time.time() < end and not self._stop.is_set():
                    time.sleep(0.1)
                sd.stop()
            finally:
                self.last_played = f"{self.playing} at {time.strftime('%H:%M:%S UTC', time.gmtime())}"
                self.playing = None
        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def due_reminders(self) -> list[str]:
        """Scheduled broadcast times (local HH:MM) that have just arrived; each fires once a day."""
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        hhmm = time.strftime("%H:%M", now)
        due = []
        for r in self.reminders:
            key = f"{today} {r}"
            if r == hhmm and key not in self._done_today:
                self._done_today.add(key)
                due.append(r)
        return due
