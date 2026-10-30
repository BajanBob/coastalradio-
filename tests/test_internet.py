"""Internet AIS (aisstream.io) client against a local stand-in WebSocket server.
Run:  python tests/test_internet.py"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import internet_ais  # noqa: E402

SAMPLES = [
    {"MessageType": "PositionReport", "MetaData": {"MMSI": 377123456, "ShipName": "CARIB TRADER  ", "latitude": 13.2, "longitude": -59.7},
     "Message": {"PositionReport": {"UserID": 377123456, "MessageID": 1, "Latitude": 13.2, "Longitude": -59.7, "Sog": 11.3,
                                    "Cog": 245.0, "TrueHeading": 246, "NavigationalStatus": 0, "Valid": True}}},
    {"MessageType": "ShipStaticData", "MetaData": {"MMSI": 377123456},
     "Message": {"ShipStaticData": {"UserID": 377123456, "Name": "CARIB TRADER@@@", "CallSign": "J8CT1", "Destination": "BRIDGETOWN",
                                    "Type": 70, "Dimension": {"A": 90, "B": 20, "C": 8, "D": 8}, "ImoNumber": 9123456, "Valid": True}}},
    {"MessageType": "StandardClassBPositionReport", "MetaData": {"MMSI": 314000222},
     "Message": {"StandardClassBPositionReport": {"UserID": 314000222, "Latitude": 13.05, "Longitude": -59.62, "Sog": 102.3,
                                                  "Cog": 360, "TrueHeading": 511, "Valid": True}}},
    {"MessageType": "PositionReport", "MetaData": {"MMSI": 970111222},
     "Message": {"PositionReport": {"UserID": 970111222, "MessageID": 1, "Latitude": 13.3, "Longitude": -59.5, "Sog": 0.5,
                                    "Cog": 90, "TrueHeading": 511, "NavigationalStatus": 14, "Valid": True}}},
    {"MessageType": "UnknownThing", "Message": {}},
]


def frame(payload: bytes, op: int = 2) -> bytes:
    n = len(payload)
    hdr = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
    return hdr + payload


def serve(port_box, got_sub):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port_box.append(srv.getsockname()[1])
    c, _ = srv.accept()
    req = b""
    while b"\r\n\r\n" not in req:
        req += c.recv(4096)
    key = [ln.split(":", 1)[1].strip() for ln in req.decode().split("\r\n") if ln.lower().startswith("sec-websocket-key")][0]
    acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
    b0, b1 = c.recv(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", c.recv(2))[0]
    mask = c.recv(4)
    data = b""
    while len(data) < n:
        data += c.recv(n - len(data))
    got_sub.append(json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data))))
    c.sendall(frame(b"hi", 9))  # ping: the client must answer and carry on
    for s in SAMPLES:
        raw = json.dumps(s).encode()
        if s is SAMPLES[1]:  # send one message in two fragments
            c.sendall(bytes([0x02, len(raw[:40])]) + raw[:40] + frame(raw[40:], 0))
        else:
            c.sendall(frame(raw))
    time.sleep(3)
    c.close()


def test_convert():
    m = internet_ais.convert(SAMPLES[0])
    assert m["mmsi"] == 377123456 and m["lat"] == 13.2 and m["sog"] == 11.3 and m["status"].startswith("Under way") and m["type"] == 1
    s = internet_ais.convert(SAMPLES[1])
    assert s["name"] == "CARIB TRADER" and s["callsign"] == "J8CT1" and s["length"] == 110 and s["shiptype_name"]
    b = internet_ais.convert(SAMPLES[2])
    assert b["cls"] == "B" and b["sog"] is None and b["cog"] is None and b["heading"] is None
    assert internet_ais.convert(SAMPLES[4]) is None
    box = internet_ais.bounding_box(13.1, -59.6, 60)
    assert abs(box[0][0][0] - 12.1) < 1e-6 and abs(box[0][1][0] - 14.1) < 1e-6 and box[0][0][1] < -59.6 < box[0][1][1]


def test_client_and_station():
    import station
    port, sub = [], []
    threading.Thread(target=serve, args=(port, sub), daemon=True).start()
    while not port:
        time.sleep(0.05)
    st = station.Station({"station": {"name": "T", "callsign": "8PFX", "lat": 13.0975, "lon": -59.6167},
                          "data_dir": tempfile.mkdtemp(), "nmea_udp": [],
                          "internet_ais": {"enabled": True, "api_key": "KEY123", "radius_nm": 50, "url": f"ws://127.0.0.1:{port[0]}/v0/stream"}})
    st.start_internet()
    for _ in range(60):
        if st.internet and st.internet.count >= 4:
            break
        time.sleep(0.1)
    assert sub and sub[0]["APIKey"] == "KEY123" and len(sub[0]["BoundingBoxes"][0]) == 2, sub
    snap = st.snapshot()
    v = {x["mmsi"]: x for x in snap["vessels"]}
    assert v[377123456]["name"] == "CARIB TRADER" and v[377123456]["is_last"] and v[377123456]["range_nm"] > 0
    assert "Internet (aisstream.io)" in v[377123456]["sources"]
    assert any(a["kind"].startswith("AIS-SART") and "internet" in a["detail"] for a in snap["alerts"])
    assert snap["internet"]["count"] >= 4 and snap["internet"]["enabled"]
    assert not st.share  # internet data never goes to sharing networks
    st.internet.stop()
    # backup mode: ignored while the radio has heard something in the last 5 minutes
    st.internet = internet_ais.InternetAis({"api_key": "", "mode": "backup"}, 13, -59, st.on_internet)
    st.internet.mode = "backup"
    st.last_rf = time.time()
    before = st.tracker.total
    st.on_internet(internet_ais.convert(SAMPLES[0]))
    assert st.tracker.total == before
    st.last_rf = 0
    st.on_internet(internet_ais.convert(SAMPLES[0]))
    assert st.tracker.total == before + 1
    # no key: clear status, no thread
    assert "API key" in internet_ais.InternetAis({}, 13, -59, print).status


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
