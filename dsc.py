"""VHF DSC (Digital Selective Calling) decoder for Channel 70: receive-only.

Follows the ITU-R M.493 structure: 1200-baud FSK on the FM audio
(1300 Hz = B/1, 2100 Hz = Y/0), 10-unit symbols (7 information bits sent
least-significant first + 3 check bits giving the number of Y bits),
phasing (DX 125 / RX 111…104), time diversity (every character sent twice,
DX and RX), and an error-check character (ECC).

Decodes distress alerts, distress acknowledgements and relays, and all-ships,
individual, group and geographic-area calls (urgency, safety, routine), with
MMSI, nature of distress, position, time and telecommands.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
from scipy.signal import lfilter

BAUD = 1200
F_B, F_Y = 1300.0, 2100.0
EOS = {117: "Acknowledge requested", 122: "Acknowledgement", 127: "End"}

FORMATS = {112: "Distress alert", 116: "All ships", 120: "Individual", 123: "Individual (semi-auto)",
           114: "Group", 102: "Geographic area"}
CATEGORIES = {100: "Routine", 108: "Safety", 110: "Urgency", 112: "Distress"}
NATURE = {100: "Fire, explosion", 101: "Flooding", 102: "Collision", 103: "Grounding",
          104: "Listing, in danger of capsizing", 105: "Sinking", 106: "Disabled and adrift",
          107: "Undesignated distress", 108: "Abandoning ship", 109: "Piracy / armed robbery",
          110: "Man overboard", 112: "EPIRB emission"}
TELECOMMAND = {100: "Voice (F3E/G3E) simplex", 101: "Voice (F3E/G3E) duplex", 103: "Polling",
               104: "Unable to comply", 105: "End of call", 106: "Data", 109: "Voice (J3E)",
               110: "Distress acknowledgement", 112: "Distress relay", 113: "Teletype FEC",
               115: "Teletype ARQ", 118: "Test", 121: "Position update", 126: "No information"}
UNABLE = {100: "No reason", 101: "Congestion", 102: "Busy", 103: "Queue", 104: "Station barred",
          105: "No operator", 106: "Operator unavailable", 107: "Equipment disabled",
          108: "Unable to use channel", 109: "Unable to use mode"}


def symbol_bits(v: int) -> list[int]:
    info = [(v >> i) & 1 for i in range(7)]
    zeros = 7 - sum(info)
    return info + [(zeros >> 2) & 1, (zeros >> 1) & 1, zeros & 1]


def decode_symbol(bits) -> int | None:
    v = sum(bits[i] << i for i in range(7))
    check = bits[7] * 4 + bits[8] * 2 + bits[9]
    return v if check == 7 - sum(bits[:7]) else None


# ------------------------------------------------------------------ message parsing
def _digits(chars) -> str | None:
    if any(c is None or c > 99 for c in chars):
        return None
    return "".join(f"{c:02d}" for c in chars)


def _mmsi(chars) -> int | None:
    d = _digits(chars)
    return int(d[:9]) if d else None


def _position(chars) -> tuple[float, float] | None:
    d = _digits(chars)
    if not d or d == "9999999999":
        return None
    q = int(d[0])
    lat = int(d[1:3]) + int(d[3:5]) / 60
    lon = int(d[5:8]) + int(d[8:10]) / 60
    if lat > 90 or lon > 180 or q > 3:
        return None
    return (lat if q in (0, 1) else -lat, lon if q in (0, 2) else -lon)


def _time(chars) -> str | None:
    d = _digits(chars)
    if not d or d == "8888" or int(d[:2]) > 23 or int(d[2:]) > 59:
        return None
    return f"{d[:2]}:{d[2:]} UTC"


def parse(c: list[int | None]) -> dict:
    """c = combined message characters, starting at the first format specifier."""
    m: dict = {"raw": c, "errors": sum(1 for x in c if x is None)}
    fmt = c[0] if c and c[0] is not None else (c[1] if len(c) > 1 else None)
    m["format_code"] = fmt
    m["format"] = FORMATS.get(fmt, f"Format {fmt}")
    i = 2

    def take(n):
        nonlocal i
        part = c[i:i + n]
        i += n
        return part + [None] * (n - len(part))

    def distress_block(prefix: str = ""):
        nat = take(1)[0]
        m[prefix + "nature_code"] = nat
        m[prefix + "nature"] = NATURE.get(nat, f"Code {nat}")
        pos = _position(take(5))
        if pos:
            m["lat"], m["lon"] = round(pos[0], 5), round(pos[1], 5)
        m["time"] = _time(take(2))
        sub = take(1)[0]
        m["subsequent"] = TELECOMMAND.get(sub, sub)

    if fmt == 112:
        m["category"] = "Distress"
        m["mmsi"] = _mmsi(take(5))
        distress_block()
    else:
        if fmt in (120, 123, 114):
            m["address"] = _mmsi(take(5))
        elif fmt == 102:
            area = _digits(take(5))
            m["area"] = area
        cat = take(1)[0]
        m["category_code"] = cat
        m["category"] = CATEGORIES.get(cat, f"Category {cat}")
        m["mmsi"] = _mmsi(take(5))
        tc1 = take(1)[0]
        m["telecommand1"] = TELECOMMAND.get(tc1, tc1)
        if cat == 112 and tc1 in (110, 112):  # distress acknowledgement / relay
            m["distress_mmsi"] = _mmsi(take(5))
            distress_block()
        else:
            tc2 = take(1)[0]
            m["telecommand2"] = (UNABLE if tc1 == 104 else TELECOMMAND).get(tc2, tc2)
            if tc1 == 121:  # position update reply
                pos = _position(take(5))
                if pos:
                    m["lat"], m["lon"] = round(pos[0], 5), round(pos[1], 5)
            else:
                f = _digits(take(3))
                if f:
                    m["frequency_raw"] = f
                    if f.startswith("90"):  # VHF channel
                        m["channel"] = str(int(f[2:]))
    eos = c[i] if i < len(c) else None
    m["eos"] = EOS.get(eos, eos)
    if eos in EOS and i + 1 < len(c) and c[i + 1] is not None and None not in c[:i]:
        ecc = c[0]
        for x in c[2:i + 1]:
            ecc ^= x
        m["ecc_ok"] = ecc == c[i + 1]
    else:
        m["ecc_ok"] = None
    m["length"] = i + 2
    return m


def summary(m: dict) -> tuple[str, str]:
    """(level, one-line description) for alerts."""
    who = f"MMSI {m.get('mmsi')}"
    if m["format_code"] == 112:
        return "crit", f"DSC DISTRESS ALERT from {who}: {m.get('nature')}"
    if m.get("category_code") == 112:
        tc = m.get("telecommand1")
        kind = "relay" if tc == TELECOMMAND[112] else "acknowledgement"
        lvl = "crit" if kind == "relay" else "warn"
        return lvl, f"DSC distress {kind} by {who} for MMSI {m.get('distress_mmsi')}: {m.get('nature')}"
    if m.get("telecommand1") == TELECOMMAND[118]:
        return "info", f"DSC test call from {who}"
    if m.get("category_code") == 110:
        return "warn", f"DSC URGENCY call ({m['format']}) from {who}"
    if m.get("category_code") == 108:
        return "info", f"DSC safety call ({m['format']}) from {who}"
    return "info", f"DSC {m['format'].lower()} call from {who}"


# ------------------------------------------------------------------ demodulator
# MF/HF DSC (2187.5, 4207.5 … 16804.5 kHz): 100 baud, 170 Hz shift. On a USB receiver tuned
# 1.7 kHz below the published frequency the tones are 1615 Hz (B/1) and 1785 Hz (Y/0).
HF = {"baud": 100, "f_b": 1615.0, "f_y": 1785.0}


class DscDemod:
    def __init__(self, fs: int, on_message: Callable[[dict], None], baud: int = BAUD,
                 f_b: float = F_B, f_y: float = F_Y) -> None:
        self.fs = fs
        self.on_message = on_message
        self.baud, self.f_b, self.f_y = baud, f_b, f_y
        self.n = 0  # running sample index (continuous mixing phase)
        win = max(4, int(round(fs / baud)))
        self.box = np.ones(win) / win
        self.zi = [np.zeros(win - 1) for _ in range(4)]
        self.inc = baud / fs
        self.phase, self.prev, self.gain = 0.0, 0.0, 0.2
        self.hunt: list[int] = []
        self.collecting = False
        self.seq: dict[int, int | None] = {}
        self.pos = 0
        self.bitbuf: list[int] = []
        self.calls = 0
        self.level_db = -120.0

    def process(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float64).ravel()
        if not len(x):
            return
        self.level_db = round(20 * math.log10(float(np.sqrt(np.mean(x * x))) + 1e-12), 1)
        t = (self.n + np.arange(len(x))) / self.fs
        self.n += len(x)
        out = []
        for k, f in enumerate((self.f_b, self.f_b, self.f_y, self.f_y)):
            mix = x * (np.cos if k % 2 == 0 else np.sin)(2 * np.pi * f * t)
            y, self.zi[k] = lfilter(self.box, 1.0, mix, zi=self.zi[k])
            out.append(y)
        d = (out[0] ** 2 + out[1] ** 2) - (out[2] ** 2 + out[3] ** 2)  # >0 → 1300 Hz → B (1)
        scale = float(np.max(np.abs(d))) or 1.0
        phase, prev, inc, gain = self.phase, self.prev, self.inc, self.gain
        for s in (d / scale).tolist():
            if (s >= 0) != (prev >= 0) and prev != s:
                cp = (phase + inc * (prev / (prev - s))) % 1.0
                phase -= gain * (cp - 0.5)
            nxt = phase + inc
            if nxt >= 1.0:
                tt = (1.0 - phase) / inc
                v = prev + (s - prev) * min(max(tt, 0.0), 1.0)
                self._bit(1 if v >= 0 else 0)
                nxt -= 1.0
            phase, prev = nxt, s
        self.phase, self.prev = phase, prev

    def _bit(self, b: int) -> None:
        if self.collecting:
            self.bitbuf.append(b)
            if len(self.bitbuf) == 10:
                self.seq[self.pos] = decode_symbol(self.bitbuf)
                self.bitbuf = []
                self.pos += 1
                if self._complete():
                    self._finish()
            return
        self.hunt.append(b)
        if len(self.hunt) > 40:
            del self.hunt[0]
        if len(self.hunt) < 40:
            return
        s = [decode_symbol(self.hunt[i * 10:(i + 1) * 10]) for i in range(4)]
        rx_ok = lambda a, b2: a is not None and b2 is not None and 105 <= a <= 111 and b2 == a - 1  # noqa: E731
        last_pos = None
        if s[0] == 125 and s[2] == 125 and rx_ok(s[1], s[3]):
            last_pos = 2 * (111 - s[3]) + 1
        elif s[1] == 125 and s[3] == 125 and rx_ok(s[0], s[2]):
            last_pos = 2 * (111 - s[2]) + 2
        elif s[1] == 125 and s[3] == 125 and s[2] is not None and 104 <= s[2] <= 110:
            last_pos = 2 * (111 - s[2]) + 2  # one phasing symbol lost to noise: three still agree
        elif s[0] == 125 and s[2] == 125 and s[1] is not None and 105 <= s[1] <= 111 and s[3] is None:
            last_pos = 2 * (111 - s[1]) + 3
        if last_pos is None:
            return
        self.seq = {last_pos - 3 + i: s[i] for i in range(4)}
        self.pos = last_pos + 1
        self.collecting, self.bitbuf, self.hunt = True, [], []

    def char(self, k: int) -> int | None:
        dx, rx = self.seq.get(12 + 2 * k), self.seq.get(17 + 2 * k)
        return dx if dx is not None else rx

    def _expected_len(self) -> int | None:
        fmt = self.char(0) if self.char(0) is not None else self.char(1)
        if fmt == 112:
            return 18
        if fmt == 116:
            cat, tc1 = self.char(2), self.char(8)
            return 25 if cat == 112 and tc1 in (110, 112) else 15
        if fmt in (120, 123, 114, 102):
            cat, tc1 = self.char(7), self.char(13)
            return 30 if cat == 112 and tc1 in (110, 112) else 20
        return None

    def _complete(self) -> bool:
        if self.pos > 160:
            return True
        n = self._expected_len()
        if n is None:
            return self.pos > 40 and self.char(0) is None and self.char(1) is None
        return self.pos > 17 + 2 * (n - 1) + 1

    def _finish(self) -> None:
        n = self._expected_len() or 0
        chars = [self.char(k) for k in range(max(n, 2))]
        self.collecting, self.seq, self.bitbuf = False, {}, []
        if n and chars and (chars[0] in FORMATS or chars[1] in FORMATS):
            m = parse(chars)
            if m.get("mmsi") is not None:
                self.calls += 1
                self.on_message(m)


# ------------------------------------------------------------------ test signal generation (for self-tests only)
def build_call(msg: list[int], eos: int = 127) -> list[int]:
    """Characters (starting with both format specifiers) → full bit sequence with phasing and time diversity."""
    ecc = msg[0]
    for x in msg[2:] + [eos]:
        ecc ^= x
    dx = [125] * 6 + msg + [eos, ecc, eos, eos]
    rx = list(range(111, 103, -1)) + msg + [eos, ecc]
    bits = [1, 0] * 10  # dot pattern
    for i in range(max(len(dx), len(rx))):
        if i < len(dx):
            bits += symbol_bits(dx[i])
        if i < len(rx):
            bits += symbol_bits(rx[i])
    return bits


def fsk_audio(bits: list[int], fs: int = 48000, amp: float = 0.4, baud: int = BAUD,
              f_b: float = F_B, f_y: float = F_Y) -> np.ndarray:
    n = int(len(bits) * fs / baud)
    idx = np.minimum((np.arange(n) * baud / fs).astype(int), len(bits) - 1)
    f = np.where(np.asarray(bits)[idx] == 1, f_b, f_y)
    return amp * np.sin(2 * np.pi * np.cumsum(f) / fs)


def mmsi_chars(mmsi: int) -> list[int]:
    d = f"{mmsi:09d}0"
    return [int(d[i:i + 2]) for i in range(0, 10, 2)]


def pos_chars(lat: float, lon: float) -> list[int]:
    q = (0 if lat >= 0 else 2) + (0 if lon >= 0 else 1)
    la, lo = abs(lat), abs(lon)
    d = f"{q}{int(la):02d}{int(round((la % 1) * 60)) % 60:02d}{int(lo):03d}{int(round((lo % 1) * 60)) % 60:02d}"
    return [int(d[i:i + 2]) for i in range(0, 10, 2)]
