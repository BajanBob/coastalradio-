"""Voice-activated recorder for SAR logging (VHF Ch16, 2182 kHz, HF working channels…).

Each transmission heard becomes a timestamped WAV file (with a short
pre-roll so the first word isn't clipped), filed by date. Old files are
removed after `retention_days`, except copies kept inside incident folders.
"""

from __future__ import annotations

import collections
import queue
import re
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np


class VoxRecorder:
    def __init__(self, cfg: dict, base: Path, on_saved: Callable[[Path, float, float, str], None]) -> None:
        self.name = cfg.get("name", "Recorder")
        self.label = re.sub(r"[^A-Za-z0-9_-]", "_", self.name)[:30]
        self.base = base
        self.fs = int(cfg.get("sample_rate", 16000))
        self.threshold = float(cfg.get("threshold_db", -38))
        self.hang = float(cfg.get("hang_seconds", 2.5))
        self.max_len = float(cfg.get("max_seconds", 600))
        self.retention = int(cfg.get("retention_days", 90))
        self.channel = int(cfg.get("audio_channel", 0))
        self.on_saved = on_saved
        self.pre = collections.deque(maxlen=int(float(cfg.get("preroll_seconds", 1.5)) * 10))
        self.q: queue.Queue = queue.Queue(maxsize=600)
        self.level_db = -120.0
        self.writer: wave.Wave_write | None = None
        self.path: Path | None = None
        self.start = 0.0
        self.last_loud = 0.0
        self.manual_until = 0.0
        self.count_today = 0
        self.error = None
        self.last_file: str | None = None
        try:
            import sounddevice as sd
            from io_ports import resolve_device
            dev = resolve_device(cfg.get("audio_device"), "input")
            info = sd.query_devices(dev, "input")
            chans = max(1, min(int(info["max_input_channels"]), self.channel + 1))
            self.stream = sd.InputStream(device=dev, channels=chans, samplerate=self.fs,
                                         blocksize=self.fs // 10, dtype="float32", callback=self._cb)
            self.stream.start()
        except Exception as e:  # noqa: BLE001
            self.error = f"Audio: {e}"
            return
        threading.Thread(target=self._run, daemon=True, name=f"rec-{self.label}").start()
        threading.Thread(target=self._cleanup_loop, daemon=True).start()

    def _cb(self, data, frames, t, status) -> None:
        try:
            self.q.put_nowait(data[:, min(self.channel, data.shape[1] - 1)].copy())
        except queue.Full:
            pass

    def record_now(self, minutes: float) -> None:
        self.manual_until = time.time() + minutes * 60

    def _open(self, now: float) -> None:
        day = self.base / time.strftime("%Y-%m-%d", time.gmtime(now))
        day.mkdir(parents=True, exist_ok=True)
        self.path = day / f"{time.strftime('%H%M%SZ', time.gmtime(now))}_{self.label}.wav"
        self.writer = wave.open(str(self.path), "wb")
        self.writer.setnchannels(1)
        self.writer.setsampwidth(2)
        self.writer.setframerate(self.fs)
        self.start = now - len(self.pre) * 0.1
        for blk in self.pre:
            self._write(blk)

    def _write(self, blk: np.ndarray) -> None:
        self.writer.writeframes((np.clip(blk, -1, 1) * 32767).astype("<i2").tobytes())

    def _close(self, now: float) -> None:
        self.writer.close()
        path, start = self.path, self.start
        self.writer, self.path = None, None
        self.count_today += 1
        self.last_file = f"{path.parent.name}/{path.name}"
        self.on_saved(path, start, now, self.name)

    def _run(self) -> None:
        while True:
            blk = self.q.get()
            now = time.time()
            rms = float(np.sqrt(np.mean(blk * blk))) if len(blk) else 0.0
            self.level_db = round(20 * np.log10(rms + 1e-9), 1)
            loud = self.level_db >= self.threshold or now < self.manual_until
            if loud:
                self.last_loud = now
            if self.writer:
                self._write(blk)
                if now - self.last_loud > self.hang or now - self.start > self.max_len:
                    self._close(now)
            elif loud:
                self._open(now)
                self._write(blk)
            else:
                self.pre.append(blk)

    def _cleanup_loop(self) -> None:
        while True:
            cutoff = time.time() - self.retention * 86400
            for day in self.base.glob("????-??-??"):
                for f in day.glob(f"*_{self.label}.wav"):
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink()
                    except OSError:
                        pass
                try:
                    if not any(day.iterdir()):
                        day.rmdir()
                except OSError:
                    pass
            time.sleep(6 * 3600)

    def status(self) -> dict:
        return {"name": self.name, "level_db": self.level_db, "recording": bool(self.writer),
                "threshold_db": self.threshold, "manual": time.time() < self.manual_until,
                "count": self.count_today, "last_file": self.last_file, "error": self.error}


def list_recordings(base: Path, limit: int = 80) -> list[dict]:
    files = sorted(base.glob("????-??-??/*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for p in files:
        try:
            with wave.open(str(p)) as w:
                dur = w.getnframes() / w.getframerate()
        except (wave.Error, OSError):
            dur = None
        out.append({"path": f"{p.parent.name}/{p.name}", "time": p.stat().st_mtime,
                    "seconds": round(dur, 1) if dur else None,
                    "source": p.stem.split("_", 1)[1].replace("_", " ") if "_" in p.stem else ""})
    return out
