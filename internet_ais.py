"""Internet AIS ("IS") from aisstream.io: ships around the station from the worldwide
receiver network, so the chart works when the radio is off or out of range.

Get a free API key at https://aisstream.io (sign in with GitHub, then "API Keys").
Messages arrive over a secure WebSocket; this file has a small built-in WebSocket
client so no extra software is needed.

Internet data is shown on the chart and checked for emergencies like radio data,
but it is never forwarded to AIS sharing networks (it came from them) and is
marked "IS" so operators know it wasn't heard by the station's own radio.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from typing import Callable

from ais import NAV_STATUS, ship_type_name

URL = "wss://stream.aisstream.io/v0/stream"


# ------------------------------------------------------------------ minimal WebSocket client (RFC 6455)
class WebSocket:
    def __init__(self, url: str, timeout: float = 20) -> None:
        u = urllib.parse.urlparse(url)
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if secure else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           "User-Agent: OceanCityWatch\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed during handshake")
            head += chunk
            if len(head) > 65536:
                raise ConnectionError("bad handshake")
        head, _, self.buf = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"server refused: {lines[0]}")
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        if hdrs.get("sec-websocket-accept") != want:
            raise ConnectionError("bad WebSocket accept key")

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, payload: bytes, opcode: int = 1) -> None:
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> bytes:
        """Next complete text/binary message (answers pings itself)."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 9:
                self.send(data, 10)
                continue
            if op == 10:
                continue
            if op == 8:
                raise ConnectionError("server closed the connection" + (f": {data[2:].decode(errors='replace')}" if len(data) > 2 else ""))
            parts.append(data)
            if b0 & 0x80:
                return b"".join(parts)

    def close(self) -> None:
        try:
            self.send(b"", 8)
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ aisstream.io JSON → decoded AIS (same fields as ais.decode)
def _num(v, bad=None, nd=5):
    if v is None or (bad is not None and v == bad):
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _pos(r: dict) -> tuple[float | None, float | None]:
    lat, lon = _num(r.get("Latitude"), 91), _num(r.get("Longitude"), 181)
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None, None
    return lat, lon


def _txt(v) -> str | None:
    s = str(v or "").replace("@", " ").strip()
    return s or None


def convert(msg: dict) -> dict | None:
    kind = msg.get("MessageType")
    body = (msg.get("Message") or {}).get(kind) or {}
    meta = {str(k).lower(): v for k, v in (msg.get("MetaData") or {}).items()}
    mmsi = body.get("UserID") or meta.get("mmsi")
    try:
        mmsi = int(mmsi)
    except (TypeError, ValueError):
        return None
    if body.get("Valid") is False:
        return None
    m: dict = {"mmsi": mmsi}
    sog = lambda: _num(body.get("Sog"), 102.3, 1)  # noqa: E731
    cog = lambda: _num(body.get("Cog"), 360, 1)  # noqa: E731
    hdg = lambda: None if body.get("TrueHeading") in (None, 511) else int(body["TrueHeading"])  # noqa: E731
    if kind == "PositionReport":
        lat, lon = _pos(body)
        st = body.get("NavigationalStatus")
        m.update(type=int(body.get("MessageID") or 1), cls="A", lat=lat, lon=lon, sog=sog(), cog=cog(), heading=hdg(),
                 status=NAV_STATUS[st] if isinstance(st, int) and 0 <= st < len(NAV_STATUS) else None)
    elif kind in ("StandardClassBPositionReport", "ExtendedClassBPositionReport"):
        lat, lon = _pos(body)
        m.update(type=18 if kind.startswith("Standard") else 19, cls="B", lat=lat, lon=lon, sog=sog(), cog=cog(),
                 heading=hdg())
        if kind.startswith("Extended"):
            m.update(name=_txt(body.get("Name")), shiptype=body.get("Type"), shiptype_name=ship_type_name(body.get("Type") or 0))
    elif kind == "ShipStaticData":
        d = body.get("Dimension") or {}
        st = body.get("Type") or 0
        m.update(type=5, cls="A", name=_txt(body.get("Name")), callsign=_txt(body.get("CallSign")),
                 destination=_txt(body.get("Destination")), imo=body.get("ImoNumber") or None,
                 shiptype=st, shiptype_name=ship_type_name(st),
                 length=(d.get("A") or 0) + (d.get("B") or 0) or None, beam=(d.get("C") or 0) + (d.get("D") or 0) or None,
                 draught=_num(body.get("MaximumStaticDraught"), 0, 1))
    elif kind == "StaticDataReport":
        a, b = body.get("ReportA") or {}, body.get("ReportB") or {}
        m.update(type=24, cls="B")
        if a.get("Valid") is not False and a.get("Name"):
            m["name"] = _txt(a.get("Name"))
        if b.get("Valid") is not False and (b.get("CallSign") or b.get("ShipType")):
            m.update(callsign=_txt(b.get("CallSign")), shiptype=b.get("ShipType"),
                     shiptype_name=ship_type_name(b.get("ShipType") or 0))
    elif kind == "BaseStationReport":
        lat, lon = _pos(body)
        m.update(type=4, cls="Base station", lat=lat, lon=lon)
    elif kind == "AidsToNavigationReport":
        lat, lon = _pos(body)
        m.update(type=21, cls="AtoN", lat=lat, lon=lon, name=_txt(body.get("Name")), aid_type="Aid to navigation")
    elif kind == "StandardSearchAndRescueAircraftReport":
        lat, lon = _pos(body)
        alt = body.get("Altitude")
        m.update(type=9, cls="SAR aircraft", lat=lat, lon=lon, sog=_num(body.get("Sog"), 1023, 0), cog=cog(),
                 altitude_m=None if alt in (None, 4095) else alt)
    elif kind == "SafetyBroadcastMessage":
        m.update(type=14, safety_text=_txt(body.get("Text")))
    elif kind == "AddressedSafetyMessage":
        m.update(type=12, safety_text=_txt(body.get("Text")), dest_mmsi=body.get("DestinationID"))
    else:
        return None
    if not m.get("name") and meta.get("shipname"):
        m["name"] = _txt(meta.get("shipname"))
    return m


def bounding_box(lat: float, lon: float, radius_nm: float) -> list:
    dlat = radius_nm / 60
    dlon = radius_nm / (60 * max(0.1, math.cos(math.radians(lat))))
    return [[[round(lat - dlat, 4), round(lon - dlon, 4)], [round(lat + dlat, 4), round(lon + dlon, 4)]]]


class InternetAis:
    """Keeps a connection to aisstream.io open (reconnecting as needed) and hands decoded messages on."""

    def __init__(self, cfg: dict, lat: float, lon: float, sink: Callable[[dict], None]) -> None:
        self.cfg = cfg
        self.key = str(cfg.get("api_key") or "").strip()
        self.radius = max(5.0, min(1000.0, float(cfg.get("radius_nm") or 100)))
        self.mode = cfg.get("mode", "always")  # always | backup (only while the radio hears nothing)
        self.url = cfg.get("url") or URL
        self.lat, self.lon, self.sink = lat, lon, sink
        self.status = "starting"
        self.connected = False
        self.count = 0
        self.last = None
        self.running = True
        self.ws: WebSocket | None = None
        if not self.key:
            self.status = "no API key: get a free one at aisstream.io and enter it in Settings"
            self.running = False
            return
        threading.Thread(target=self._run, daemon=True, name="internet-ais").start()

    def stop(self) -> None:
        self.running = False
        if self.ws:
            self.ws.close()

    def _run(self) -> None:
        delay = 5
        while self.running:
            try:
                self.status = "connecting…"
                self.ws = WebSocket(self.url)
                self.ws.send(json.dumps({"APIKey": self.key,
                                         "BoundingBoxes": bounding_box(self.lat, self.lon, self.radius)}).encode())
                self.ws.sock.settimeout(90)
                self.connected, delay = True, 5
                self.status = f"connected · ships within {self.radius:g} nm"
                while self.running:
                    raw = self.ws.recv()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("error"):
                        raise ConnectionError(str(msg["error"]))
                    m = convert(msg) if isinstance(msg, dict) else None
                    if m:
                        self.count += 1
                        self.last = time.time()
                        self.sink(m)
            except socket.timeout:
                self.status = "connected, no ships reported in the last 90 s (reconnecting)"
            except Exception as e:  # noqa: BLE001
                self.status = f"offline: {e} · retrying in {delay} s"
            self.connected = False
            if self.ws:
                self.ws.close()
                self.ws = None
            for _ in range(delay):
                if not self.running:
                    return
                time.sleep(1)
            delay = min(delay * 2, 120)

    def info(self) -> dict:
        return {"enabled": True, "status": self.status, "connected": self.connected, "count": self.count,
                "last": self.last, "radius_nm": self.radius, "mode": self.mode}
