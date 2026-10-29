"""Demo signals for the HF console, radiosondes and data feeds (python ocw.py --demo).

Everything is generated as audio or UDP messages and goes through the real
decoders: band noise with a carrier whistle (auto-notch), voice-like
transmissions on busy channels (scan stops, recorder runs), SELCAL calls on
aviation channels, an HF DSC distress alert on 8414.5 kHz, an RS41 radiosonde
climbing from Grantley Adams and bursting, and dumphfdl / JAERO / Scytale-C
style messages over UDP.
"""

from __future__ import annotations

import json
import math
import random
import socket
import threading
import time

import numpy as np
from scipy.signal import butter, lfilter

import dsc
import selcal
import sonde

FS = 48000
BUSY_KHZ = {8291, 5550, 8846, 6577, 4125, 3016, 8906, 6604, 13306}  # channels with simulated traffic


class HfDemo:
    def __init__(self, rx) -> None:
        self.rx = rx
        self.rng = np.random.default_rng()
        self.b, self.a = butter(4, [250, 3000], btype="band", fs=FS)
        self.zi = np.zeros(max(len(self.a), len(self.b)) - 1)
        self.queue: list[np.ndarray] = []
        self.next_event = time.time() + 4
        self.selcals = ["AB-CD", "FK-MS", "BH-JQ", "CE-LR"]
        self.dsc_sent = False
        threading.Thread(target=self._run, daemon=True, name="hf-demo").start()

    def _voice(self, seconds: float) -> np.ndarray:
        t = np.arange(int(FS * seconds)) / FS
        pitch = 130 + 25 * np.sin(2 * math.pi * 0.7 * t)
        ph = 2 * math.pi * np.cumsum(pitch) / FS
        v = sum(np.sin(k * ph) / k for k in range(1, 18))
        env = np.clip(np.sin(2 * math.pi * 3.5 * t + self.rng.uniform(0, 6)) + 0.3, 0, None) ** 1.5
        v = lfilter(*butter(2, [400, 2600], btype="band", fs=FS), v * env)
        return 0.25 * v / (np.max(np.abs(v)) + 1e-9)

    def _plan(self) -> None:
        ch = self.rx.channel or {}
        khz = self.rx.dial_hz / 1000
        kind = ch.get("kind")
        now = time.time()
        if kind == "dsc" and abs(khz - 8412.8) < 0.2 and not self.dsc_sent:
            self.dsc_sent = True
            call = [112, 112] + dsc.mmsi_chars(636091234) + [106] + dsc.pos_chars(14.35, -56.8) \
                + [int(time.strftime("%H", time.gmtime())), int(time.strftime("%M", time.gmtime())), 109]
            self.queue.append(dsc.fsk_audio([1, 0] * 100 + dsc.build_call(call), FS, 0.25, **dsc.HF))
        elif ch.get("khz") in BUSY_KHZ or round(khz) in BUSY_KHZ:
            if kind in ("aviation", "volmet") and random.random() < 0.6:
                self.queue.append(selcal.tone_audio(random.choice(self.selcals), FS, 0.2))
            self.queue.append(self._voice(random.uniform(3, 7)))
        self.next_event = now + random.uniform(6, 14)

    def _run(self) -> None:
        n = FS // 10
        t0 = time.time()
        k = 0
        carrier_ph = 0.0
        while True:
            if time.time() >= self.next_event and not self.queue:
                self._plan()
            noise, self.zi = lfilter(self.b, self.a, self.rng.normal(0, 0.12, n), zi=self.zi)
            blk = noise
            # a steady heterodyne whistle on some channels, for the auto-notch to find
            if int(self.rx.dial_hz / 1000) % 3 == 0:
                tt = np.arange(n) / FS
                blk = blk + 0.06 * np.sin(2 * math.pi * 1180 * tt + carrier_ph)
                carrier_ph = (carrier_ph + 2 * math.pi * 1180 * n / FS) % (2 * math.pi)
            if self.queue:
                q = self.queue[0]
                take, self.queue[0] = q[:n], q[n:]
                blk = blk.copy()
                blk[:len(take)] += take
                if not len(self.queue[0]):
                    self.queue.pop(0)
            self.rx.feed(blk)
            k += 1
            sleep = t0 + k * 0.1 - time.time()
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -2:  # fell behind: resync instead of racing
                t0, k = time.time(), 0


class SondeDemo:
    """A radiosonde launched from Grantley Adams (sped up so the burst happens within a few minutes)."""

    def __init__(self, sonde_rx, lat: float = 13.0746, lon: float = -59.4925) -> None:
        self.rx = sonde_rx
        self.lat, self.lon = lat, lon
        self.alt, self.climb = 18000.0, 45.0
        self.frame = 2100
        self.rng = np.random.default_rng()
        threading.Thread(target=self._run, daemon=True, name="sonde-demo").start()

    def _run(self) -> None:
        while True:
            if self.climb > 0 and self.alt > 26500:
                self.climb = -38.0  # burst
            if self.alt < 200:
                self.alt, self.climb, self.lat, self.lon = 18000.0, 45.0, 13.0746, -59.4925  # next launch
            self.alt += self.climb
            self.lon -= 0.0011  # trade-wind drift to the west
            self.lat += 0.0002
            ve, vn = -32 / 3.6 * 3, 8 / 3.6 * 3
            fr = sonde.build_frame("T3540127", self.frame, self.lat, self.lon, self.alt, ve, vn, self.climb)
            a = sonde.frame_audio(fr, FS, 0.3)
            a = np.concatenate([a, np.zeros(FS - len(a))]) + self.rng.normal(0, 0.06, FS)
            for i in range(0, FS, 4800):
                self.rx.demod.process(a[i:i + 4800])
            self.frame += 1
            time.sleep(1)


class FeedDemo:
    """dumphfdl / JAERO / Scytale-C style messages sent over UDP to the feed listeners."""

    FLIGHTS = [("BAW2155", "G-VIIL", 14.8, -48.0, 260), ("AAL1385", "N838NN", 19.5, -62.0, 150),
               ("VIR31", "G-VNEW", 16.2, -45.5, 255), ("DLH418", "D-AIXB", 22.0, -55.0, 230),
               ("CWC812", "9Y-TTA", 12.0, -57.0, 40)]

    def __init__(self, ports: dict[str, int]) -> None:
        self.ports = ports
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.t0 = time.time()
        self.flights = [list(f) for f in self.FLIGHTS]
        threading.Thread(target=self._run, daemon=True, name="feed-demo").start()

    def _send(self, kind: str, obj) -> None:
        if kind in self.ports:
            data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.sock.sendto(data, ("127.0.0.1", self.ports[kind]))

    def _run(self) -> None:
        n = 0
        while True:
            time.sleep(4)
            n += 1
            f = random.choice(self.flights)
            f[2] += 0.12 * math.cos(math.radians(f[4]))
            f[3] += 0.12 * math.sin(math.radians(f[4]))
            gs = random.choice([("Shannon, Ireland", 8942), ("Riverhead, New York", 8912),
                                ("Santa Cruz, Bolivia", 8957), ("Canarias, Spain", 8948)])
            acars = {"reg": "." + f[1], "flight": f[0][:2] + f[0][3:], "label": "H1", "msg_text": ""}
            if n % 5 == 0:
                acars["label"] = "AA"
                acars["arinc622"] = {"cpdlc": {"atc_uplink_msg": {"header": {"msg_id": n},
                                     "elements": [{"msg_element": random.choice(
                                         ["CLIMB TO AND MAINTAIN FL380", "PROCEED DIRECT TO PIREX",
                                          "CONTACT NEW YORK CENTER 8846", "REPORT PASSING 15N050W"])}]}}}
            elif n % 3 == 0:
                acars["msg_text"] = f"POSRPT {f[2]:.1f}N/{-f[3]:.1f}W FL370 ETA TBPB 1840"
            self._send("hfdl", {"hfdl": {"t": {"sec": int(time.time()), "usec": 0}, "freq": gs[1] * 1000,
                                         "bit_rate": 1800, "sig_level": round(random.uniform(-38, -18), 1),
                                         "noise_level": -45.2,
                                         "lpdu": {"src": {"type": "Aircraft", "id": random.randint(1, 200)},
                                                  "dst": {"type": "Ground station", "id": 7, "name": gs[0]},
                                                  "hfnpdu": {"flight_id": f[0], "pos": {"lat": round(f[2], 3),
                                                                                        "lon": round(f[3], 3)},
                                                             "acars": acars}}}})
            if n % 4 == 1:
                self._send("acars", {"timestamp": time.time(), "app": {"name": "JAERO"}, "tail": "N838NN",
                                     "flight": "AA1385", "label": "Q0", "freq": 1545.07,
                                     "text": "OOOI OUT 1612 OFF 1625 KTUL"})
            if n % 6 == 2:
                self._send("inmarsat", random.choice([
                    b"NAVAREA IV 0911/26. CARIBBEAN SEA. BARBADOS. CARLISLE BAY LIGHTED BUOY 2 UNLIT. CANCEL THIS MSG 302359Z OCT 26.",
                    b"SECURITE. HIGH SEAS FORECAST. TROPICAL N ATLANTIC 07N TO 22N. E WINDS 15 TO 20 KT. SEAS 6 TO 8 FT.",
                ]))
            if n == 20:
                self._send("inmarsat", json.dumps({"les": "Burum LES", "priority": "Urgency", "message":
                           "PAN PAN. RCC FORT DE FRANCE. FISHING VESSEL OVERDUE SINCE 27 OCT, LAST SEEN 13-20N 058-50W. "
                           "VESSELS IN AREA KEEP SHARP LOOKOUT AND REPORT TO RCC."}).encode())
