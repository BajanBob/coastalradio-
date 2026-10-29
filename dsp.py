"""Computer signal processing for SSB receive audio.

The radio demodulates SSB; this cleans up what it hands the computer:
  - decimates the sound card audio to 12 kHz (the SSB passband is under 3 kHz)
  - noise reduction: per-frequency noise floor tracking + Wiener-style gain
  - auto-notch: finds steady carriers/whistles and removes them
  - band-pass 250-3000 Hz and AGC, so weak and strong stations sound alike
  - waterfall rows and signal/noise figures for the dashboard
  - voice activity (used by scanning and the recorder)

Streaming short-time FFT with 50 % overlap-add (sqrt-Hann windows), 512-point
frames at 12 kHz: ~23 Hz resolution, ~43 ms latency.
"""

from __future__ import annotations

import collections
import math
import threading

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import firwin, lfilter

FS = 12000
N = 512
HOP = N // 2
BIN_HZ = FS / N
LO_BIN, HI_BIN = int(250 / BIN_HZ), int(3000 / BIN_HZ)
VOICE = slice(int(300 / BIN_HZ), int(2700 / BIN_HZ))
WATERFALL_BINS = 96  # 0-3000 Hz


class Decimator:
    """Any sound card rate that is a multiple of 12 kHz → 12 kHz, keeping filter state across blocks."""

    def __init__(self, fs_in: int) -> None:
        if fs_in % FS:
            raise ValueError(f"sample rate {fs_in} must be a multiple of 12000 (use 48000)")
        self.q = fs_in // FS
        self.taps = firwin(8 * self.q + 1, 5600, fs=fs_in) if self.q > 1 else np.array([1.0])
        self.zi = np.zeros(len(self.taps) - 1)
        self.phase = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.q == 1:
            return x
        y, self.zi = lfilter(self.taps, 1.0, x, zi=self.zi)
        out = y[self.phase::self.q]
        self.phase = (self.phase - len(x)) % self.q
        return out


class SsbDsp:
    def __init__(self, fs_in: int = 48000, nr: bool = True, notch: bool = True, agc: bool = True,
                 nr_strength: float = 1.6) -> None:
        self.dec = Decimator(fs_in)
        self.nr, self.notch, self.agc = nr, notch, agc
        self.nr_strength = nr_strength
        self.win = np.sqrt(np.hanning(N + 1)[:N])  # periodic sqrt-Hann: analysis * synthesis sums to 1 at 50 %
        self.inbuf = np.zeros(0)
        self.prev_tail = np.zeros(HOP)
        self.last_frame = np.zeros(N)
        nb = N // 2 + 1
        self.P = np.full(nb, 1e-8)       # smoothed power
        self.noise = np.full(nb, 1e-8)   # noise floor estimate
        self.longavg = np.full(nb, 1e-8)  # slow average for steady tones
        self.gain = np.ones(nb)
        self.agc_gain = 1.0
        self.frames = 0
        self.rows: collections.deque = collections.deque(maxlen=240)  # (index, [0..255]*96)
        self.row_index = 0
        self._acc = np.zeros(nb)
        self._acc_n = 0
        self.level_db = -120.0
        self.snr_db = 0.0
        self.snr_s = 0.0
        self.base = 0.0
        self.voice = False
        self.notched_hz: list[int] = []
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- public
    def process(self, x: np.ndarray) -> np.ndarray:
        """Sound-card block in → processed 12 kHz audio out (same duration)."""
        x = self.dec(np.asarray(x, dtype=np.float64).ravel())
        if len(x):
            self.level_db = round(20 * math.log10(float(np.sqrt(np.mean(x * x))) + 1e-12), 1)
        self.inbuf = np.concatenate([self.inbuf, x])
        out = []
        while len(self.inbuf) >= HOP:
            hop, self.inbuf = self.inbuf[:HOP], self.inbuf[HOP:]
            frame = np.concatenate([self.last_frame[HOP:], hop])
            self.last_frame = frame
            y = self._frame(frame)
            out.append(self.prev_tail + y[:HOP])
            self.prev_tail = y[HOP:]
        if not out:
            return np.zeros(0)
        y = np.concatenate(out)
        return self._agc(y) if self.agc else y

    def waterfall_since(self, index: int) -> list:
        with self.lock:
            return [r for r in self.rows if r[0] > index]

    # ---------------------------------------------------------------- internals
    def _frame(self, frame: np.ndarray) -> np.ndarray:
        X = np.fft.rfft(frame * self.win)
        p = (X.real ** 2 + X.imag ** 2) + 1e-12
        self.frames += 1
        self.P = 0.6 * self.P + 0.4 * p
        # noise floor: follows quiet moments down at once, creeps up ~1.5 dB/s otherwise
        self.noise = np.where(self.P < self.noise, self.P, self.noise * 1.008)
        if self.frames <= 10:  # start-up: take the first ~0.2 s as the noise floor
            self.noise = self.P.copy()
        self.longavg = 0.995 * self.longavg + 0.005 * p  # ~4 s: carriers yes, 1 s SELCAL tones no

        g = np.ones_like(p)
        if self.nr:
            g = np.clip(1.0 - self.nr_strength * self.noise / self.P, 0.12, 1.0)
            self.gain = np.maximum(g, 0.7 * self.gain)  # release slowly: fewer "musical" artefacts
            g = self.gain
        if self.notch:
            g = g * self._notch_mask()
        g = g.copy()
        g[:LO_BIN] = 0.0
        g[HI_BIN:] = 0.0

        # signal above noise in the voice band. The raw ratio reads a few dB even on pure noise
        # (the floor tracker sits at the noise minimum), so subtract a slowly learned baseline,
        # and hold peaks briefly so the gaps between syllables don't count as silence.
        sig = float(np.sum(self.P[VOICE]))
        nse = float(np.sum(self.noise[VOICE])) or 1e-12
        raw = 10 * math.log10(max(sig / nse, 1e-6))
        if self.frames <= 20:
            self.base = raw if self.frames == 1 else 0.8 * self.base + 0.2 * raw
        elif not self.voice and raw - self.base < 2.0:  # learn only from noise-like frames
            self.base += 0.004 * (raw - self.base)
        elif raw < self.base:
            self.base += 0.02 * (raw - self.base)
        self.snr_s = max(raw - self.base, self.snr_s - 0.25)  # ~12 dB/s fall
        self.snr_db = round(max(0.0, self.snr_s), 1)
        self.voice = self.snr_db > (2.5 if self.voice else 4.0)

        self._acc += p
        self._acc_n += 1
        if self._acc_n >= 9:  # ≈ 5 waterfall rows a second
            self._push_row(self._acc / self._acc_n)
            self._acc[:] = 0
            self._acc_n = 0
        return np.fft.irfft(X * g, N) * self.win

    def _notch_mask(self) -> np.ndarray:
        m = np.ones_like(self.longavg)
        if self.frames < 40:
            return m
        seg = self.longavg[LO_BIN:HI_BIN]
        ref = np.median(seg) + 1e-12
        # a steady tone stands far above the slow average of its neighbours
        local = median_filter(self.longavg, size=25, mode="nearest") + 1e-12  # median ignores the tone itself
        tones = np.where((self.longavg > 15 * local) & (self.longavg > 15 * ref))[0]
        tones = [t for t in tones if LO_BIN <= t < HI_BIN]
        for t in tones:
            m[max(0, t - 1):t + 2] = 0.03
        self.notched_hz = sorted({int(round(t * BIN_HZ, -1)) for t in tones})[:6]
        return m

    def _agc(self, y: np.ndarray) -> np.ndarray:
        out = np.empty_like(y)
        g = self.agc_gain
        target = 0.12
        for i in range(0, len(y), 120):  # 10 ms steps
            blk = y[i:i + 120]
            peak = float(np.max(np.abs(blk))) + 1e-9
            want = min(target / peak, 2000.0)
            g = want if want < g else g + (want - g) * 0.02  # fast attack, slow (~0.5 s) release
            out[i:i + 120] = blk * g
        self.agc_gain = g
        return np.clip(out, -0.95, 0.95)

    def _push_row(self, p: np.ndarray) -> None:
        bins = p[:int(3000 / BIN_HZ)]
        k = len(bins) // WATERFALL_BINS
        b = bins[:k * WATERFALL_BINS].reshape(WATERFALL_BINS, k).mean(axis=1)
        db = 10 * np.log10(b)
        floor = float(np.percentile(db, 20))
        row = np.clip((db - floor + 6) * 6, 0, 255).astype(int).tolist()
        with self.lock:
            self.row_index += 1
            self.rows.append((self.row_index, row))
