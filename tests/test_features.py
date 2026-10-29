"""Tests for DSC decoding, traffic watch, coverage, composer and AI drafting.
Run:  python tests/test_features.py"""

import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dsc  # noqa: E402
from watch import cpa_tcpa, point_in_polygon  # noqa: E402


def _dsc_run(msg, fs=48000, noise=0.1, seed=3):
    rng = np.random.default_rng(seed)
    got = []
    d = dsc.DscDemod(fs, got.append)
    bits = dsc.build_call(msg)
    sig = np.concatenate([rng.normal(0, noise, fs // 2), dsc.fsk_audio(bits, fs) + rng.normal(0, noise, int(len(bits) * fs / 1200)),
                          rng.normal(0, noise, fs)])
    for i in range(0, len(sig), fs // 10):
        d.process(sig[i:i + fs // 10])
    return got


def test_dsc_distress():
    msg = [112, 112] + dsc.mmsi_chars(314123456) + [105] + dsc.pos_chars(13.2, -59.68) + [11, 52, 100]
    for fs, noise in ((48000, 0.05), (44100, 0.15)):
        got = _dsc_run(msg, fs, noise)
        assert len(got) == 1
        m = got[0]
        assert m["mmsi"] == 314123456 and m["nature"] == "Sinking" and m["ecc_ok"]
        assert abs(m["lat"] - 13.2) < 0.01 and abs(m["lon"] + 59.6833) < 0.01 and m["time"] == "11:52 UTC"
        assert dsc.summary(m)[0] == "crit"


def test_dsc_other_calls():
    urg = [116, 116, 110] + dsc.mmsi_chars(3140001) + [100, 126, 90, 0, 16]
    m = _dsc_run(urg)[0]
    assert m["category"] == "Urgency" and m["channel"] == "16" and dsc.summary(m)[0] == "warn"
    relay = [116, 116, 112] + dsc.mmsi_chars(3140001) + [112] + dsc.mmsi_chars(314123456) + [110] + \
        dsc.pos_chars(13.1, -59.6) + [12, 0, 100]
    m = _dsc_run(relay)[0]
    assert m["distress_mmsi"] == 314123456 and m["nature"] == "Man overboard" and dsc.summary(m)[0] == "crit"
    test = [120, 120] + dsc.mmsi_chars(3140001) + [108] + dsc.mmsi_chars(377123456) + [118, 126, 126, 126, 126]
    m = _dsc_run(test)[0]
    assert m["address"] == 3140001 and dsc.summary(m)[0] == "info"


def test_geometry():
    a = {"lat": 13.0, "lon": -59.70, "sog": 10, "cog": 90}
    b = {"lat": 13.0, "lon": -59.60, "sog": 10, "cog": 270}
    cpa, t = cpa_tcpa(a, b)
    assert cpa < 0.01 and 17 < t < 18  # 5.85 nm apart closing at 20 kn → 17.5 min
    c = {"lat": 13.1, "lon": -59.60, "sog": 10, "cog": 90}
    assert cpa_tcpa(a, c)[1] < 0 or cpa_tcpa(a, c)[0] > 5  # parallel, far apart
    sq = [[13, -60], [13, -59], [14, -59], [14, -60]]
    assert point_in_polygon(13.5, -59.5, sq) and not point_in_polygon(12.5, -59.5, sq)


class _Api(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Api.seen.append((dict(self.headers), body))
        out = {"type": "message", "content": [{"type": "text", "text": "SECURITE, SECURITE, SECURITE. ALL STATIONS. [MISSING: buoy colour]"}]}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    def log_message(self, *a):
        pass


def test_station_features():
    import synth
    from station import Station
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = tempfile.mkdtemp()
    st = Station({"station": {"name": "OCEAN CITY RADIO", "callsign": "8PFX", "lat": 13.0975, "lon": -59.6167},
                  "data_dir": data, "nmea_udp": [], "traffic_watch": {"dark_minutes_a": 1.0},
                  "ai": {"api_key": "test-key", "endpoint": f"http://127.0.0.1:{srv.server_port}/v1/messages"}})
    st.watch.add_zone({"name": "Test Reef", "type": "reef", "lat": 13.0, "lon": -59.68, "radius_nm": 1})
    st.watch.add_watch(377000301, "Ship of interest", "check papers", "warn")
    # converging pair, 3.5 nm apart at 20 kn closing → CPA ~0 in ~10 min; one is inside the reef zone
    for _ in range(5):
        st.on_bits(synth.type1(377000301, 13.0, -59.68, 10, 90, 90), "t", "A")
        st.on_bits(synth.type1(377000302, 13.0, -59.62, 10, 270, 270), "t", "B")
    # anchored ship dragging 300 m
    st.on_bits(synth.type1(377000303, 13.08, -59.62, 0.1, 0, 511, status=1), "t", "A")
    st.on_bits(synth.type1(377000303, 13.0827, -59.62, 0.4, 0, 511, status=1), "t", "A")
    st.tick()
    kinds = {a["kind"] for a in st.alerts.snapshot()}
    assert {"Geofence", "Watchlist", "Collision risk", "Anchor watch"} <= kinds, kinds
    # dark vessel: last report > limit ago while moving
    st.tracker.vessels[377000302]["last"] -= 120
    st._last_watch_tick = 0
    st.tick()
    assert any(a["kind"] == "AIS gap" for a in st.alerts.snapshot())
    snap = st.snapshot()
    assert snap["watch"]["coverage"]["cells"] and max(snap["watch"]["coverage"]["sectors"]) > 0
    assert snap["channels"].get("A") is not None and snap["channels"].get("B") is not None
    # DSC distress via the station → incident on the chart
    st.on_dsc(dsc.parse([112, 112] + dsc.mmsi_chars(314777888) + [101] + dsc.pos_chars(13.0, -59.5) + [12, 30, 100, 127, 0]), "DSC test")
    snap = st.snapshot()
    assert any(a["kind"] == "DSC distress" and a["incident"] for a in snap["alerts"])
    assert any(v["mmsi"] == 314777888 and v.get("emergency") for v in snap["vessels"])
    # composer templates
    import composer
    t = composer.compose("securite", st.info, {"title": "Navigational warning", "text": "Unlit buoy adrift", "lat": "13.1", "lon": "-59.65"})
    assert t.startswith("SECURITE, SECURITE, SECURITE") and "EIGHT PAPA FOXTROT X-RAY" in t and "UNLIT BUOY ADRIFT" in t
    relay = composer.compose("mayday_relay", st.info, {}, st.incidents)
    assert relay.startswith("MAYDAY RELAY") and "3 1 4 7 7 7 8 8 8" in relay
    # AI drafting uses the API correctly and returns the text untouched
    text = composer.ai_draft(st.cfg["ai"], st.info, "securite", "buoy adrift west of the harbour", {"x": 1})
    assert "[MISSING: buoy colour]" in text
    hdr, body = _Api.seen[-1]
    hdr = {k.lower(): v for k, v in hdr.items()}  # HTTP header names are case-insensitive
    assert hdr.get("x-api-key") == "test-key" and hdr.get("anthropic-version")
    assert body["model"] and body["messages"][0]["role"] == "user" and "8PFX" in body["system"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
