"""SELCAL decoder (ICAO Annex 10): aviation HF selective calling.

A SELCAL call is two tone pairs: tones 1+2 together for ~1 s, a ~0.2 s gap,
then tones 3+4 for ~1 s. Each tone is one of 16 letters. The code is written
as e.g. "AB-CD" (letters in each pair are sent in alphabetical order).

Works on the receiver's USB audio (unprocessed, so the auto-notch can't
remove the tones). Uses 16 tuned filters (100 ms windows, 50 ms steps):
long enough to separate A and B, which are only 34 Hz apart.
"""

from __future__ import annotations

import math
import time
from typing import Callable

import numpy as np

TONES = {"A": 312.6, "B": 346.7, "C": 384.6, "D": 426.6, "E": 473.2, "F": 524.8, "G": 582.1, "H": 645.7,
         "J": 716.1, "K": 794.3, "L": 881.0, "M": 977.2, "P": 1083.9, "Q": 1202.3, "R": 1333.5, "S": 1479.1}
LETTERS = list(TONES)


def valid(code: str) -> bool:
    c = code.replace("-", "").upper()
    return (len(c) == 4 and all(x in TONES for x in c) and c[0] < c[1] and c[2] < c[3]
            and len(set(c)) == 4)


class SelcalDecoder:
    BLOCK_S = 0.05   # step
    WINDOW_S = 0.10  # analysis window

    def __init__(self, fs: int, on_call: Callable[[str], None]) -> None:
        self.fs = fs
        self.on_call = on_call
        self.n = int(fs * self.BLOCK_S)
        self.w = int(fs * self.WINDOW_S)
        self.buf = np.zeros(0)
        self.hist = np.zeros(self.w)
        t = np.arange(self.w) / fs
        win = np.hanning(self.w)
        self.basis = np.array([win * np.exp(-2j * math.pi * f * t) for f in TONES.values()])
        self.pair: str | None = None
        self.pair_blocks = 0
        self.misses = 0
        self.first: tuple[str, float] | None = None  # (pair, end time)
        self.gap_blocks = 0
        self.calls = 0
        self.last_code: str | None = None
        self.last_time = 0.0
        self.clock = 0.0  # seconds of audio processed

    def process(self, x: np.ndarray) -> None:
        self.buf = np.concatenate([self.buf, np.asarray(x, dtype=np.float64).ravel()])
        while len(self.buf) >= self.n:
            step, self.buf = self.buf[:self.n], self.buf[self.n:]
            self.hist = np.concatenate([self.hist[self.n:], step])
            self.clock += self.BLOCK_S
            self._block(self.hist)

    def _block(self, blk: np.ndarray) -> None:
        e = np.abs(self.basis @ blk) ** 2
        # filter energy a pure tone carrying all the block's power would give (Hann window)
        total = float(np.mean(blk * blk)) * self.w * self.w / 8 + 1e-12
        order = np.argsort(e)[::-1]
        a, b, c = e[order[0]], e[order[1]], e[order[2]]
        # two strong tones of similar level, clearly above every other letter and holding most of the energy
        med = float(np.median(e)) + 1e-12
        top = "".join(sorted((LETTERS[order[0]], LETTERS[order[1]])))
        strict = b > 0.25 * a and b > 6 * c and b > 25 * med and (a + b) > 0.12 * total
        loose = b > 0.15 * a and b > 3 * c and b > 10 * med  # enough to carry on a pair already started
        if self.pair and top == self.pair and (strict or loose):
            self.pair_blocks += 1 + self.misses
            self.misses = 0
            return
        if self.pair and self.misses < 2 and not strict:  # one or two blocks lost to fading/noise
            self.misses += 1
            return
        self._end_pair()
        self.misses = 0
        self.pair = top if strict else None
        self.pair_blocks = 1 if strict else 0
        if not strict:
            self.gap_blocks += 1
            if self.first and self.gap_blocks * self.BLOCK_S > 0.6:
                self.first = None

    def _end_pair(self) -> None:
        if not self.pair:
            return
        dur = self.pair_blocks * self.BLOCK_S
        if 0.6 <= dur <= 1.6:  # nominal 1.0 ± 0.25 s, allow for fading
            if self.first and self.gap_blocks * self.BLOCK_S <= 0.6 and self.first[0] != self.pair:
                code = f"{self.first[0]}-{self.pair}"
                self.first = None
                if valid(code) and not (code == self.last_code and self.clock - self.last_time < 5):
                    self.calls += 1
                    self.last_code, self.last_time = code, self.clock
                    self.on_call(code)
            else:
                self.first = (self.pair, self.clock)
        self.gap_blocks = self.misses
        self.pair, self.pair_blocks = None, 0


def tone_audio(code: str, fs: int = 48000, amp: float = 0.25, lead: float = 0.3) -> np.ndarray:
    """Test signal: the SELCAL call for `code` (for self-tests and the demo)."""
    c = code.replace("-", "").upper()
    t = np.arange(int(fs * 1.0)) / fs

    def pair(x, y):
        return amp * (np.sin(2 * math.pi * TONES[x] * t) + np.sin(2 * math.pi * TONES[y] * t)) / 2
    z = lambda s: np.zeros(int(fs * s))  # noqa: E731
    return np.concatenate([z(lead), pair(c[0], c[1]), z(0.2), pair(c[2], c[3]), z(lead)])


