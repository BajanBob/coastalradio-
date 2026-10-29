"""Vaisala RS41 radiosonde decoder (400-406 MHz weather balloons).

Input: FM discriminator audio (NFM, ~12-15 kHz wide, NO de-emphasis) from an
SDR program (SDR++, SDR#, SDRuno) through a virtual audio cable, or from a
scanner's discriminator/9600 data output. 48 kHz sample rate.

RS41 air interface: 4800 baud GFSK, frames of 320 bytes (518 with extra data)
once a second, bytes sent least-significant bit first, an 8-byte sync header,
the whole frame XOR-whitened with a 64-byte mask, then a series of blocks
[type, length, data…, CRC-16] (CRC-16/CCITT-FALSE, stored little-endian).

Decoded: sonde serial, frame number, GPS position/altitude (ECEF → lat/lon),
climb rate, ground speed and heading, satellites, GPS time, and a rough
landing estimate once the balloon has burst. Only CRC-checked blocks are used.
Constants follow the open documentation in the rs1729/RS decoder project.
"""

from __future__ import annotations

import math
import time
from typing import Callable

import numpy as np
from scipy.signal import firwin, lfilter

BAUD = 4800
HEADER_AIR = bytes([0x10, 0xB6, 0xCA, 0x11, 0x22, 0x96, 0x12, 0xF8])  # sync as sent on air
MASK = bytes([
    0x96, 0x83, 0x3E, 0x51, 0xB1, 0x49, 0x08, 0x98, 0x32, 0x05, 0x59, 0x0E, 0xF9, 0x44, 0xC6, 0x26,
    0x21, 0x60, 0xC2, 0xEA, 0x79, 0x5D, 0x6D, 0xA1, 0x54, 0x69, 0x47, 0x0C, 0xDC, 0xE8, 0x5C, 0xF1,
    0xF7, 0x76, 0x82, 0x7F, 0x07, 0x99, 0xA2, 0x2C, 0x93, 0x7C, 0x30, 0x63, 0xF5, 0x10, 0x2E, 0x61,
    0xD0, 0xBC, 0xB4, 0xB6, 0x06, 0xAA, 0xF4, 0x23, 0x78, 0x6E, 0x3B, 0xAE, 0xBF, 0x7B, 0x4C, 0xC1])
NDATA, XDATA = 320, 518
POS_FRAMETYPE, POS_BLOCKS = 0x38, 0x39
BLK_STATUS, BLK_GPSINFO, BLK_GPSPOS = 0x79, 0x7C, 0x7B
GPS_EPOCH = 315964800  # 1980-01-06 UTC
LEAP = 18              # GPS - UTC seconds (since 2017)


def _bits_lsb(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


HEADER_BITS = np.array(_bits_lsb(HEADER_AIR), dtype=np.int8)


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def ecef_to_geo(x: float, y: float, z: float) -> tuple[float, float, float]:
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    b = a * (1 - f)
    ep2 = (a * a - b * b) / (b * b)
    p = math.hypot(x, y)
    th = math.atan2(z * a, p * b)
    lat = math.atan2(z + ep2 * b * math.sin(th) ** 3, p - e2 * a * math.cos(th) ** 3)
    lon = math.atan2(y, x)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), alt


def enu_velocity(lat: float, lon: float, vx: float, vy: float, vz: float) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    e = -math.sin(lo) * vx + math.cos(lo) * vy
    n = -math.sin(la) * math.cos(lo) * vx - math.sin(la) * math.sin(lo) * vy + math.cos(la) * vz
    u = math.cos(la) * math.cos(lo) * vx + math.cos(la) * math.sin(lo) * vy + math.sin(la) * vz
    return e, n, u


def parse_frame(frame: bytes) -> dict | None:
    """Whitened-removed frame bytes → decoded fields (None if no block passed its CRC)."""
    out: dict = {"blocks_ok": 0, "blocks_bad": 0}
    pos, end = POS_BLOCKS, len(frame)
    while pos + 4 <= end:
        typ, ln = frame[pos], frame[pos + 1]
        if pos + 4 + ln > end:
            break
        body = frame[pos + 2:pos + 2 + ln]
        stored = frame[pos + 2 + ln] | frame[pos + 3 + ln] << 8
        if crc16(body) != stored:
            out["blocks_bad"] += 1
            pos += 4 + ln
            continue
        out["blocks_ok"] += 1
        if typ == BLK_STATUS and ln >= 10:
            out["frame"] = body[0] | body[1] << 8
            sid = body[2:10].decode("ascii", "replace")
            if sid.isalnum():
                out["serial"] = sid
        elif typ == BLK_GPSINFO and ln >= 6:
            week = body[0] | body[1] << 8
            tow = int.from_bytes(body[2:6], "little") / 1000.0
            out["utc"] = GPS_EPOCH + week * 604800 + tow - LEAP
        elif typ == BLK_GPSPOS and ln >= 19:
            x, y, z = (int.from_bytes(body[i:i + 4], "little", signed=True) / 100.0 for i in (0, 4, 8))
            vx, vy, vz = (int.from_bytes(body[i:i + 2], "little", signed=True) / 100.0 for i in (12, 14, 16))
            out["sats"] = body[18]
            if x or y or z:
                lat, lon, alt = ecef_to_geo(x, y, z)
                e, n, u = enu_velocity(lat, lon, vx, vy, vz)
                out.update(lat=round(lat, 5), lon=round(lon, 5), alt=round(alt), climb=round(u, 1),
                           speed_kmh=round(math.hypot(e, n) * 3.6, 1),
                           heading=round(math.degrees(math.atan2(e, n)) % 360))
        pos += 4 + ln
    return out if out["blocks_ok"] else None


class Rs41Demod:
    def __init__(self, fs: int, on_frame: Callable[[dict], None]) -> None:
        self.fs = fs
        self.on_frame = on_frame
        # low-pass at ~0.75 x baud: best eye opening for GFSK BT 0.5 in noise
        self.taps = firwin(41, 3600, fs=fs)
        self.zi = np.zeros(len(self.taps) - 1)
        self.inc = BAUD / fs
        self.phase, self.prev, self.gain = 0.0, 0.0, 0.06
        self.dc = 0.0
        self.shift = np.zeros(64, dtype=np.int8)
        self.collect: list[int] | None = None
        self.invert = False
        self.want = NDATA * 8
        self.frames = 0
        self.bad = 0
        self.level_db = -120.0
        self.last_serial: str | None = None

    def process(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float64).ravel()
        if not len(x):
            return
        self.level_db = round(20 * math.log10(float(np.sqrt(np.mean(x * x))) + 1e-12), 1)
        y, self.zi = lfilter(self.taps, 1.0, x, zi=self.zi)
        # remove the receiver's frequency offset (DC on the discriminator) with a slow average
        dc = self.dc
        a = 1.0 - math.exp(-1.0 / (0.25 * self.fs))
        ys = np.empty_like(y)
        for i, v in enumerate(y.tolist()):
            dc += a * (v - dc)
            ys[i] = v - dc
        self.dc = dc
        phase, prev, inc, gain = self.phase, self.prev, self.inc, self.gain
        for s in ys.tolist():
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
        if self.collect is not None:
            self.collect.append(b ^ self.invert)
            n = len(self.collect)
            if n == (POS_FRAMETYPE + 1) * 8:
                ft = self._byte(POS_FRAMETYPE)
                self.want = XDATA * 8 if ft == 0xF0 else NDATA * 8
            if n >= self.want:
                self._finish()
            return
        self.shift[:-1] = self.shift[1:]
        self.shift[-1] = b
        d = int(np.sum(self.shift != HEADER_BITS))
        if d <= 3 or d >= 61:
            self.invert = d >= 61
            self.collect = [int(v) for v in HEADER_BITS]
            self.want = NDATA * 8

    def _byte(self, i: int) -> int:
        bits = self.collect[i * 8:(i + 1) * 8]
        return sum(bit << k for k, bit in enumerate(bits)) ^ MASK[i % 64]

    def _finish(self) -> None:
        nbytes = len(self.collect) // 8
        frame = bytes(self._byte(i) for i in range(nbytes))
        self.collect = None
        info = parse_frame(frame)
        if info and info.get("serial"):
            self.last_serial = info["serial"]
        elif info and self.last_serial:
            info["serial"] = self.last_serial  # status block lost to noise this frame
        if info:
            self.frames += 1
            self.on_frame(info)
        else:
            self.bad += 1


class SondeTracker:
    """Keeps each sonde's latest fix and track, works out burst and a rough landing point."""

    def __init__(self, timeout_s: float = 3 * 3600) -> None:
        self.sondes: dict[str, dict] = {}
        self.timeout = timeout_s

    def update(self, f: dict, source: str = "") -> tuple[dict | None, str | None]:
        """Returns (sonde record, event) where event is 'new', 'burst' or None."""
        sid = f.get("serial")
        if not sid or "lat" not in f:
            return None, None
        now = time.time()
        s = self.sondes.get(sid)
        event = None
        if not s:
            s = self.sondes[sid] = {"serial": sid, "first": now, "track": [], "max_alt": 0, "burst": None,
                                    "source": source}
            event = "new"
        s.update({k: f[k] for k in ("frame", "lat", "lon", "alt", "climb", "speed_kmh", "heading", "sats", "utc")
                  if k in f})
        s["last"] = now
        if f["alt"] > s["max_alt"]:
            s["max_alt"] = f["alt"]
        if not s["burst"] and s["max_alt"] > 3000 and f.get("climb", 0) < -3 and f["alt"] < s["max_alt"] - 300:
            s["burst"] = {"alt": s["max_alt"], "t": now}
            event = "burst"
        if not s["track"] or now - s["track"][-1][2] >= 10:
            s["track"].append((f["lat"], f["lon"], now, f["alt"]))
            del s["track"][:-720]
        s["landing"] = self._landing(s)
        return s, event

    @staticmethod
    def _landing(s: dict) -> dict | None:
        climb = s.get("climb") or 0
        if climb > -2 or s.get("alt") is None:
            return None
        secs = s["alt"] / -climb  # crude: constant descent rate, current drift (under-estimates slowing parachute)
        dist = (s.get("speed_kmh") or 0) / 3.6 * secs
        hd = math.radians(s.get("heading") or 0)
        lat = s["lat"] + dist * math.cos(hd) / 111_320
        lon = s["lon"] + dist * math.sin(hd) / (111_320 * max(0.2, math.cos(math.radians(s["lat"]))))
        return {"lat": round(lat, 4), "lon": round(lon, 4), "minutes": round(secs / 60, 1)}

    def snapshot(self) -> list[dict]:
        now = time.time()
        for k in [k for k, s in self.sondes.items() if now - s["last"] > self.timeout]:
            self.sondes.pop(k)
        return sorted(({k: v for k, v in s.items()} | {"age": round(now - s["last"])}
                       for s in self.sondes.values()), key=lambda s: s["age"])


# ------------------------------------------------------------------ test signal (self-tests and demo)
def build_frame(serial: str, frame_no: int, lat: float, lon: float, alt: float,
                ve: float = 5.0, vn: float = 2.0, vu: float = 5.0, utc: float | None = None) -> bytes:
    """A plain (un-whitened) 320-byte RS41 frame with status, GPS info and GPS position blocks."""
    def block(typ, body):
        c = crc16(body)
        return bytes([typ, len(body)]) + body + bytes([c & 0xFF, c >> 8])
    la, lo = math.radians(lat), math.radians(lon)
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    x = (n + alt) * math.cos(la) * math.cos(lo)
    y = (n + alt) * math.cos(la) * math.sin(lo)
    z = (n * (1 - e2) + alt) * math.sin(la)
    vx = -math.sin(lo) * ve - math.sin(la) * math.cos(lo) * vn + math.cos(la) * math.cos(lo) * vu
    vy = math.cos(lo) * ve - math.sin(la) * math.sin(lo) * vn + math.cos(la) * math.sin(lo) * vu
    vz = math.cos(la) * vn + math.sin(la) * vu
    utc = time.time() if utc is None else utc
    gps = utc - GPS_EPOCH + LEAP
    week, tow = int(gps // 604800), int((gps % 604800) * 1000)
    status = frame_no.to_bytes(2, "little") + serial.encode()[:8].ljust(8, b"0") + bytes(30)
    gpsinfo = week.to_bytes(2, "little") + tow.to_bytes(4, "little") + bytes(24)
    gpspos = (b"".join(int(round(v * 100)).to_bytes(4, "little", signed=True) for v in (x, y, z))
              + b"".join(int(round(v * 100)).to_bytes(2, "little", signed=True) for v in (vx, vy, vz))
              + bytes([9, 0]))
    body = block(BLK_STATUS, status) + block(BLK_GPSINFO, gpsinfo) + block(BLK_GPSPOS, gpspos)
    frame = bytearray(NDATA)
    frame[0:8] = bytes(h ^ m for h, m in zip(HEADER_AIR, MASK))
    frame[POS_FRAMETYPE] = 0x0F
    frame[POS_BLOCKS:POS_BLOCKS + len(body)] = body
    rest = POS_BLOCKS + len(body)
    pad = NDATA - rest - 4
    frame[rest:] = block(0x76, bytes(pad))
    return bytes(frame)


def frame_audio(frame: bytes, fs: int = 48000, amp: float = 0.3) -> np.ndarray:
    """Plain frame → whitened bits → GFSK-like discriminator audio, with a short preamble."""
    air = bytes(b ^ MASK[i % 64] for i, b in enumerate(frame))
    bits = [0, 1] * 40 + _bits_lsb(air)
    sps = fs / BAUD
    n = int(len(bits) * sps)
    idx = np.minimum((np.arange(n) / sps).astype(int), len(bits) - 1)
    nrz = np.where(np.asarray(bits)[idx] == 1, 1.0, -1.0)
    sigma = math.sqrt(math.log(2)) / (2 * math.pi * 0.5) * sps  # Gaussian filter, BT = 0.5
    g = np.exp(-0.5 * (np.arange(-15, 16) / sigma) ** 2)
    return amp * np.convolve(nrz, g / g.sum(), mode="same")
