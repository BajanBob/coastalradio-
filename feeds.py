"""Data feeds from companion decoder programs, received over UDP on this computer
or your local network:

  hfdl      dumphfdl (HFDL / HF ACARS / CPDLC / ADS-C), e.g. on a Raspberry Pi or WSL with an SDR:
              dumphfdl --soapysdr driver=airspyhf --sample-rate 912000 --centerfreq 8900 8912 8927 8942 \\
                       --output decoded:json:udp:address=<this PC>,port=5555
  acars     any ACARS decoder that sends JSON over UDP (JAERO's UDP/JSON output for Inmarsat
              Aero, acarsdec, vdlm2dec…)
  inmarsat  Scytale-C (Inmarsat-C: EGC SafetyNET, NAVAREA warnings, distress relays). JSON or
              plain text lines are both accepted.

The companion program does the demodulation; Ocean City Watch parses the messages,
puts aircraft on the chart, keeps a message log, and raises alerts for distress
and urgency traffic. JSON field names differ between programs and versions, so
the parser looks for the meaning (registration, flight, position, text, CPDLC…)
anywhere in the message rather than relying on one fixed layout.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections import deque
from typing import Callable

DISTRESS_RE = re.compile(r"\b(MAYDAY|DISTRESS|EPIRB|PLB ACTIVAT|SART|ABANDON(ING)? SHIP|SINKING|MAN OVERBOARD|"
                         r"7700|HIJACK|SQUAWK 7500)\b", re.I)
URGENCY_RE = re.compile(r"\b(PAN[ -]?PAN|URGENCY|SEARCH AND RESCUE|\bSAR\b|OVERDUE|MISSING|MEDEVAC|"
                        r"7600|DIVERT(ING)?|EMERGENCY)\b", re.I)


def _walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, o


def _find(flat: list[tuple[str, object]], *names: str):
    for name in names:
        for path, v in flat:
            leaf = re.sub(r"\[\d+\]", "", path).rsplit(".", 1)[-1].lower()
            if leaf == name and v not in (None, "", []):
                return v
    return None


def _position(msg) -> tuple[float, float] | None:
    """First {lat, lon} pair anywhere in the message (dumphfdl 'pos', ADS-C reports, JAERO…)."""
    stack = [msg]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            la = o.get("lat", o.get("latitude"))
            lo = o.get("lon", o.get("lng", o.get("longitude")))
            if isinstance(la, (int, float)) and isinstance(lo, (int, float)) and (la or lo) \
                    and -90 <= la <= 90 and -180 <= lo <= 180:
                return float(la), float(lo)
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def _sub(msg, key: str):
    stack = [msg]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if key in str(k).lower():
                    return v
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def _compact(o, limit: int = 600) -> str:
    """Readable one-line rendering of a decoded structure (e.g. a CPDLC message)."""
    parts = []
    for path, v in _walk(o):
        if isinstance(v, (str, int, float)) and str(v).strip():
            leaf = re.sub(r"\[\d+\]", "", path).split(".")[-1]
            parts.append(f"{leaf}={v}")
    s = " · ".join(parts)
    return s[:limit] + ("…" if len(s) > limit else "")


def parse(kind: str, raw: bytes) -> dict | None:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        msg = json.loads(text)
    except ValueError:
        msg = None
    ev = {"t": time.time(), "feed": kind}
    if not isinstance(msg, dict):  # plain-text decoder output (e.g. Scytale-C text mode)
        ev.update(text=text[:4000], title=text.splitlines()[0][:120])
    else:
        flat = list(_walk(msg))
        reg = _find(flat, "reg", "tail", "registration")
        flight = _find(flat, "flight", "flight_id", "flight_number", "callsign")
        body = _find(flat, "msg_text", "text", "message", "msg", "body", "content")
        ev.update(
            reg=str(reg).lstrip(".") if reg else None,
            flight=str(flight).strip() if flight else None,
            icao=_find(flat, "icao", "icao24", "aircraft_id", "ac_id"),
            label=_find(flat, "label"),
            freq=_find(flat, "freq", "frequency"),
            sig=_find(flat, "sig_level", "level", "signal", "snr"),
            station=_find(flat, "gs", "ground_station", "les", "lesname", "les_name", "station", "sat", "satname"),
            priority=_find(flat, "priority", "prioritytext", "priority_text", "category"),
            msgtype=_find(flat, "messagetype", "message_type", "service") if kind == "inmarsat" else None,
            text=str(body)[:4000] if body is not None else None,
        )
        if not ev["station"]:  # dumphfdl: name of the ground station end of the link
            ev["station"] = next((v for p, v in flat if re.search(r"(src|dst)\.name$", p)), None)
        pos = _position(msg)
        if pos:
            ev["lat"], ev["lon"] = pos
        cpdlc = _sub(msg, "cpdlc")
        if cpdlc:
            ev["cpdlc"] = _compact(cpdlc)
        adsc = _sub(msg, "adsc") or _sub(msg, "ads_c")
        if adsc:
            ev["adsc"] = _compact(adsc, 300)
        if isinstance(ev.get("freq"), (int, float)) and ev["freq"] > 100000:
            ev["freq"] = round(ev["freq"] / 1000, 1)  # Hz → kHz
        who = ev.get("flight") or ev.get("reg") or ev.get("icao")
        what = "CPDLC" if cpdlc else ("ADS-C" if adsc else ev.get("label") and f"label {ev['label']}")
        ev["title"] = " · ".join(str(x) for x in (who, what, ev.get("text") and ev["text"][:80]) if x) \
            or _compact(msg, 120)
        ev["raw"] = msg
    blob = " ".join(str(ev.get(k) or "") for k in ("text", "priority", "cpdlc", "title", "msgtype"))
    ev["level"] = "crit" if DISTRESS_RE.search(blob) else "warn" if URGENCY_RE.search(blob) else "info"
    return ev


class FeedListener:
    """Receives one companion program's UDP output."""

    def __init__(self, kind: str, port: int, sink: Callable[[dict], None], bind: str = "0.0.0.0") -> None:
        self.kind, self.port, self.sink = kind, port, sink
        self.count = 0
        self.last = None
        self.error = None
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((bind, port))
        except OSError as e:
            self.error = f"UDP port {port}: {e}"
            return
        threading.Thread(target=self._run, daemon=True, name=f"feed-{kind}").start()

    def _run(self) -> None:
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except OSError:
                time.sleep(1)
                continue
            for chunk in (data.split(b"\n") if data.lstrip().startswith(b"{") and b"}\n{" in data else [data]):
                try:
                    ev = parse(self.kind, chunk)
                except Exception as e:  # noqa: BLE001
                    ev = {"t": time.time(), "feed": self.kind, "level": "info", "title": f"(unparsed: {e})",
                          "text": chunk[:500].decode("utf-8", "replace")}
                if ev:
                    self.count += 1
                    self.last = time.time()
                    self.sink(ev)

    def status(self) -> dict:
        return {"kind": self.kind, "port": self.port, "count": self.count, "last": self.last, "error": self.error}


class AircraftTracker:
    """Aircraft heard on HFDL / ACARS with a position, for the chart."""

    def __init__(self, timeout_s: float = 2 * 3600) -> None:
        self.ac: dict[str, dict] = {}
        self.timeout = timeout_s

    def update(self, ev: dict) -> None:
        key = ev.get("reg") or ev.get("icao") or ev.get("flight")
        if not key:
            return
        a = self.ac.setdefault(str(key), {"key": str(key), "first": ev["t"], "track": [], "msgs": 0})
        a["msgs"] += 1
        a["last"] = ev["t"]
        for k in ("reg", "flight", "icao", "feed", "freq", "station"):
            if ev.get(k):
                a[k] = ev[k]
        if ev.get("lat") is not None:
            a["lat"], a["lon"] = ev["lat"], ev["lon"]
            a["track"].append((ev["lat"], ev["lon"], ev["t"]))
            del a["track"][:-200]

    def snapshot(self) -> list[dict]:
        now = time.time()
        for k in [k for k, a in self.ac.items() if now - a["last"] > self.timeout]:
            self.ac.pop(k)
        return sorted(self.ac.values(), key=lambda a: -a["last"])


AI_TRIAGE = """You help the radio operator at {name} ({callsign}), a coast station at {lat}, {lon}.
You get one decoded message from an Inmarsat-C SafetyNET / ACARS / HFDL feed.
Reply in at most three short plain-text lines, no markdown:
1) What it is, in plain words (translate abbreviations).
2) Any position mentioned (decimal degrees) and its distance and bearing from the station if you can work it out.
3) Whether the station should act (distress/urgency in or near its area) or just note it.
Never invent details that are not in the message."""


class Triage:
    """Optional: asks Claude to explain distress/urgency messages (needs the "ai" key in config.json)."""

    def __init__(self, ai_cfg: dict, station: dict, enabled: bool) -> None:
        self.cfg, self.st = ai_cfg or {}, station
        self.on = enabled and bool(self.cfg.get("api_key"))
        self.q: deque = deque(maxlen=20)
        if self.on:
            threading.Thread(target=self._run, daemon=True, name="ai-triage").start()

    def submit(self, ev: dict, levels=("crit", "warn")) -> None:
        if self.on and ev.get("level") in levels:
            ev["ai"] = "AI is reading this message…"
            self.q.append(ev)

    def explain(self, ev: dict) -> str:
        import composer
        system = AI_TRIAGE.format(name=self.st.get("name", ""), callsign=self.st.get("callsign", ""),
                                  lat=self.st.get("lat"), lon=self.st.get("lon"))
        content = {k: v for k, v in ev.items() if k not in ("raw", "ai")}
        return composer.ai_call(self.cfg, system, json.dumps(content, default=str)[:6000], 300)

    def _run(self) -> None:
        while True:
            if not self.q:
                time.sleep(0.5)
                continue
            ev = self.q.popleft()
            try:
                ev["ai"] = self.explain(ev)
            except Exception as e:  # noqa: BLE001
                ev["ai"] = f"(AI unavailable: {e})"
