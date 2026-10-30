"""HF SSB receiver console and radiosonde receiver.

HfReceiver: the radio (IC-9100, FT-991A, IC-7300, …) demodulates USB; the
computer tunes it by CAT, then cleans the audio (noise reduction, auto-notch,
AGC), shows a waterfall, plays it on the PC speakers, scans channel lists,
stops on voice and records each transmission. SELCAL and HF DSC decoders run
on the same (unprocessed) audio all the time.

SondeReceiver: RS41 decoder fed from SDR/scanner discriminator audio.

Receive-only, like the rest of Coastal Radio: frequency and mode only.
"""

from __future__ import annotations

import collections
import queue
import re
import threading
import time
import wave
from pathlib import Path

import numpy as np

import bands
import dsc
import selcal
import sonde
from cat import Rig, fmt_freq, in_coverage, make_rig
from dsp import FS as DSP_FS, SsbDsp
from io_ports import AudioInput, WavInput


class AudioOut:
    """Plays processed receive audio on the PC speakers (optional)."""

    def __init__(self, device, fs_out: int = 48000) -> None:
        import sounddevice as sd
        from io_ports import resolve_device
        dev = resolve_device(device, "output")
        self.q: queue.Queue = queue.Queue(maxsize=40)
        self.fs = fs_out
        self.muted = False
        self.volume = 0.8
        self.stream = sd.OutputStream(device=dev, channels=1, samplerate=fs_out, dtype="float32")
        self.stream.start()
        threading.Thread(target=self._run, daemon=True, name="hf-audio-out").start()

    def play(self, y12k: np.ndarray) -> None:
        if self.muted or not len(y12k):
            return
        try:
            self.q.put_nowait(y12k)
        except queue.Full:
            pass

    def _run(self) -> None:
        from scipy.signal import resample_poly
        while True:
            y = self.q.get()
            out = resample_poly(y, self.fs // DSP_FS, 1).astype("float32") * self.volume
            try:
                self.stream.write(out.reshape(-1, 1))
            except Exception:  # noqa: BLE001
                time.sleep(0.1)


class HfReceiver:
    def __init__(self, station, cfg: dict) -> None:
        self.station = station
        self.cfg = cfg
        self.name = cfg.get("name", "HF receiver")
        self.model = (cfg.get("model") or cfg.get("rig") or "").lower().replace("-", "")
        self.fs = int(cfg.get("sample_rate", 48000))
        self.dsp = SsbDsp(self.fs, nr=cfg.get("noise_reduction", True), notch=cfg.get("auto_notch", True),
                          agc=cfg.get("agc", True))
        self.selcal = selcal.SelcalDecoder(self.fs, self._selcal)
        self.hfdsc = dsc.DscDemod(self.fs, self._dsc, **dsc.HF)
        self.channels = bands.channels(cfg.get("extra_channels"))
        self.lists = bands.scan_lists()
        self.lists.update({k: v for k, v in (cfg.get("scan_lists") or {}).items() if isinstance(v, list)})
        self.selcal_watch = {k.upper(): v for k, v in (cfg.get("selcal_watch") or {}).items()}
        self.vox_db = float(cfg.get("voice_snr_db", 4))
        self.hang = float(cfg.get("hang_seconds", 4))
        self.dwell = float(cfg.get("scan_dwell_seconds", 2.5))
        self.record = bool(cfg.get("record", True))
        self.rec_dir: Path = station.rec_dir
        self.error = None
        self.out = None
        self.dial_hz = 0
        self.mode = "USB"
        self.channel: dict | None = None
        self.scan_name: str | None = None
        self.scan_freqs: list[dict] = []
        self.scan_i = 0
        self.on_freq_since = time.time()
        self.voice_since = None
        self.last_voice = 0.0
        self.holding = False
        self.writer = None
        self.rec_path: Path | None = None
        self.rec_start = 0.0
        self.pre = collections.deque(maxlen=12)  # ~1.2 s pre-roll of processed audio
        self.selcal_log: collections.deque = collections.deque(maxlen=100)
        self.heard: collections.deque = collections.deque(maxlen=100)  # transmissions heard (freq, time, length)
        self.lock = threading.Lock()
        try:
            self.rig: Rig = make_rig(cfg)
        except Exception as e:  # noqa: BLE001
            self.rig = Rig(self.name)
            self.error = f"CAT: {e}"
        if cfg.get("output_device") not in (None, "", "none"):
            try:
                self.out = AudioOut(cfg["output_device"])
            except Exception as e:  # noqa: BLE001
                self.error = f"Speaker output: {e}"
        start = cfg.get("start_khz", 8291)
        self.tune_khz(float(start), cfg.get("start_mode", "USB"))
        if cfg.get("wav"):
            WavInput(cfg["wav"], self.feed, loop=bool(cfg.get("loop")))
        elif cfg.get("audio_device") is not None:
            try:
                AudioInput(cfg["audio_device"], int(cfg.get("audio_channel", 0)), self.fs, self.feed)
            except Exception as e:  # noqa: BLE001
                self.error = f"Audio: {e}"

    # ------------------------------------------------------------ tuning
    def tune_khz(self, khz: float, mode: str = "USB", offset_khz: float = 0.0, channel: dict | None = None) -> str:
        mode = mode.upper()
        if mode not in ("USB", "LSB", "AM", "FM"):
            raise ValueError("mode must be USB, LSB, AM or FM")
        hz = int(round((khz + offset_khz) * 1000))
        if not 30_000 <= hz <= 3_000_000_000:
            raise ValueError("frequency out of range")
        if not in_coverage(self.model, hz):
            raise ValueError(f"{self.name} ({self.model.upper()}) can't receive {fmt_freq(hz)}")
        self._close_rec()
        self.dial_hz, self.mode = hz, mode
        self.channel = channel or next((c for c in self.channels if abs(c["dial_khz"] * 1000 - hz) < 50), None)
        self.on_freq_since = time.time()
        self.holding = False
        self.voice_since = None
        self.dsp = SsbDsp(self.fs, nr=self.dsp.nr, notch=self.dsp.notch, agc=self.dsp.agc)  # fresh noise estimate
        try:
            self.rig.tune(hz, mode)
        except Exception as e:  # noqa: BLE001
            self.rig.status = f"CAT error: {e}"
        return self.rig.status

    def tune_channel(self, cid: str) -> str:
        ch = next((c for c in self.channels if c["id"] == cid), None)
        if not ch:
            raise ValueError("unknown channel")
        self.stop_scan()
        return self.tune_khz(ch["khz"], ch["mode"], ch["dial_khz"] - ch["khz"], ch)

    def start_scan(self, name: str) -> None:
        if name not in self.lists:
            raise ValueError("unknown scan list")
        freqs = self.lists[name]
        self.scan_freqs = [next((c for c in self.channels if c["khz"] == f), None)
                           or {"khz": f, "dial_khz": f, "mode": "USB", "label": f"{f:g}", "group": name}
                           for f in freqs]
        self.scan_name, self.scan_i = name, 0
        self._scan_tune()

    def stop_scan(self) -> None:
        self.scan_name, self.scan_freqs = None, []

    def _scan_tune(self) -> None:
        ch = self.scan_freqs[self.scan_i % len(self.scan_freqs)]
        try:
            self.tune_khz(ch["khz"], ch["mode"], ch["dial_khz"] - ch["khz"], ch if "id" in ch else None)
        except ValueError as e:
            self.rig.status = str(e)

    def tick(self) -> None:
        now = time.time()
        if self.scan_name and not self.holding and now - self.on_freq_since >= self.dwell:
            self.scan_i = (self.scan_i + 1) % len(self.scan_freqs)
            self._scan_tune()

    def set_dsp(self, **kw) -> None:
        for k in ("nr", "notch", "agc"):
            if k in kw:
                setattr(self.dsp, k, bool(kw[k]))
        if "nr_strength" in kw:
            self.dsp.nr_strength = max(0.5, min(3.0, float(kw["nr_strength"])))
        if self.out and "volume" in kw:
            self.out.volume = max(0.0, min(2.0, float(kw["volume"])))
        if self.out and "mute" in kw:
            self.out.muted = bool(kw["mute"])

    # ------------------------------------------------------------ audio
    def feed(self, x: np.ndarray) -> None:
        self.selcal.process(x)
        self.hfdsc.process(x)
        y = self.dsp.process(x)
        if self.out:
            self.out.play(y)
        now = time.time()
        # ignore the first second after retuning (radio and noise estimate settling)
        thr = self.vox_db if not self.holding else self.vox_db - 1.5  # hysteresis
        voice = self.dsp.snr_db >= thr and now - self.on_freq_since > 1.0
        if voice:
            self.voice_since = self.voice_since or now
            self.last_voice = now
            if now - self.voice_since >= 0.4 and not self.holding:
                self.holding = True
                if self.record:
                    self._open_rec(now)
        else:
            self.voice_since = None
            if self.holding and now - self.last_voice > self.hang:
                self.holding = False
                self._close_rec()
        if self.writer:
            self._write(y)
        else:
            self.pre.append(y)

    def _label(self) -> str:
        return f"HF_{self.dial_hz / 1000:g}kHz"

    def _open_rec(self, now: float) -> None:
        day = self.rec_dir / time.strftime("%Y-%m-%d", time.gmtime(now))
        day.mkdir(parents=True, exist_ok=True)
        self.rec_path = day / f"{time.strftime('%H%M%SZ', time.gmtime(now))}_{self._label()}.wav"
        self.writer = wave.open(str(self.rec_path), "wb")
        self.writer.setnchannels(1)
        self.writer.setsampwidth(2)
        self.writer.setframerate(DSP_FS)
        self.rec_start = now - 0.1 * len(self.pre)
        for blk in self.pre:
            self._write(blk)
        self.pre.clear()

    def _write(self, y: np.ndarray) -> None:
        self.writer.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())

    def _close_rec(self) -> None:
        now = time.time()
        if self.holding or self.writer:
            khz = self.dial_hz / 1000
            ch = self.channel
            entry = {"t": self.rec_start or now, "khz": khz, "seconds": round(now - (self.rec_start or now), 1),
                     "label": f"{ch['group']} {ch['label']}" if ch else f"{khz:g} kHz {self.mode}"}
            with self.lock:
                self.heard.appendleft(entry)
            if not self.writer:  # recorded ones appear in the timeline as recordings
                self.station.event("hf", self.name, f"Transmission on {khz:g} kHz {self.mode} "
                                   f"({entry['label']}), {entry['seconds']} s", "info")
        if not self.writer:
            return
        self.writer.close()
        path, start = self.rec_path, self.rec_start
        self.writer, self.rec_path, self.rec_start = None, None, 0.0
        self.station._on_recording(path, start, now, f"{self.name} {self.dial_hz / 1000:g} kHz")

    # ------------------------------------------------------------ decoders
    def _selcal(self, code: str) -> None:
        khz = self.dial_hz / 1000
        ch = self.channel
        entry = {"t": time.time(), "code": code, "khz": khz, "where": ch["group"] if ch else "",
                 "watch": self.selcal_watch.get(code)}
        with self.lock:
            self.selcal_log.appendleft(entry)
        self.station.on_selcal(entry, self.name)

    def _dsc(self, m: dict) -> None:
        self.station.on_dsc(m, f"HF DSC {self.dial_hz / 1000 + 1.7:g} kHz")

    # ------------------------------------------------------------ dashboard
    def status(self) -> dict:
        d = self.dsp
        ch = self.channel
        return {
            "name": self.name, "model": self.model.upper(), "rig_status": self.error or self.rig.status,
            "cat_ok": self.rig.ok and not self.error, "dial_khz": self.dial_hz / 1000, "mode": self.mode,
            "freq": fmt_freq(self.dial_hz), "channel": ch and {k: ch[k] for k in ("id", "group", "label", "khz", "kind")},
            "scan": self.scan_name, "holding": self.holding, "recording": bool(self.writer),
            "level_db": d.level_db, "snr_db": d.snr_db, "voice": d.voice, "notched_hz": d.notched_hz,
            "nr": d.nr, "notch": d.notch, "agc": d.agc, "nr_strength": d.nr_strength,
            "speaker": bool(self.out), "muted": bool(self.out and self.out.muted),
            "volume": self.out.volume if self.out else None, "row_index": d.row_index,
            "selcal": list(self.selcal_log)[:30], "heard": list(self.heard)[:30],
            "selcal_calls": self.selcal.calls, "dsc_calls": self.hfdsc.calls,
        }

    def catalog(self) -> dict:
        return {"channels": self.channels, "lists": self.lists}


class SondeReceiver:
    """RS41 radiosonde receiver from discriminator audio (SDR program → virtual audio cable)."""

    def __init__(self, station, cfg: dict) -> None:
        self.station = station
        self.name = cfg.get("name", "Radiosonde")
        self.mhz = cfg.get("frequency_mhz")
        self.fs = int(cfg.get("sample_rate", 48000))
        self.demod = sonde.Rs41Demod(self.fs, self._frame)
        self.error = None
        self.last = None
        if cfg.get("wav"):
            WavInput(cfg["wav"], self.demod.process, loop=bool(cfg.get("loop")))
        elif cfg.get("audio_device") is not None:
            try:
                AudioInput(cfg["audio_device"], int(cfg.get("audio_channel", 0)), self.fs, self.demod.process)
            except Exception as e:  # noqa: BLE001
                self.error = f"Audio: {e}"

    def _frame(self, f: dict) -> None:
        self.last = time.time()
        self.station.on_sonde(f, self.name)

    def status(self) -> dict:
        return {"name": self.name, "mhz": self.mhz, "level_db": self.demod.level_db, "frames": self.demod.frames,
                "bad": self.demod.bad, "last": self.last, "error": self.error}


def safe_label(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40]
