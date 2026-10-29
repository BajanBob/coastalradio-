"""Tests for emergency detection, incidents, station position formats and forwarding.
Run:  python tests/test_station.py"""

import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import synth  # noqa: E402
from ais import classify, decode  # noqa: E402
from announce import dm, maidenhead, phonetic, script  # noqa: E402


def test_emergency_classification():
    sart = decode(synth.type1(970123456, 13.2, -59.7, 0.5, 90, 511, status=14))
    assert classify(sart)[0] == "crit" and "SART" in classify(sart)[1]
    test = decode(synth.type14(970123456, "SART TEST"))
    assert classify(test)[0] == "info"
    mob = decode(synth.type14(972111222, "MOB ACTIVE"))
    assert classify(mob)[0] == "crit" and "MOB" in classify(mob)[1]
    epirb = decode(synth.type1(974555666, 13.2, -59.7, 0, 0, 511, status=14))
    assert "EPIRB" in classify(epirb)[1]
    sar = decode(synth.type9(111234567, 13.1, -59.5, 300, 120, 45))
    assert sar["cls"] == "SAR aircraft" and sar["altitude_m"] == 300 and classify(sar)[0] == "info"
    normal = decode(synth.type1(377123456, 13.1, -59.6, 10, 90, 90))
    assert classify(normal) is None
    safety = decode(synth.type14(377123456, "NAV WARNING BUOY ADRIFT"))
    assert classify(safety)[0] == "warn"


def test_position_formats():
    assert maidenhead(13.0975, -59.6167) == "GK03ec"   # Barbados is in GK03
    assert maidenhead(51.4779, -0.0015) == "IO91xl"    # Greenwich, a known reference
    la, lo = dm(13.0975, -59.6167)
    assert la == "13°05.850'N" and lo == "059°37.002'W"
    assert phonetic("8PFX") == "EIGHT PAPA FOXTROT X-RAY"
    s = script({"name": "OCEAN CITY RADIO", "callsign": "8PFX", "lat": 13.0975, "lon": -59.6167})
    assert "EIGHT PAPA FOXTROT X-RAY" in s["text"] and "GOLF KILO ZERO THREE ECHO CHARLIE" in s["text"]


class _Hook(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        _Hook.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_station_emergency_flow():
    from station import Station
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = tempfile.mkdtemp()
    st = Station({"station": {"name": "OCEAN CITY RADIO", "callsign": "8PFX", "lat": 13.0975, "lon": -59.6167},
                  "data_dir": data, "nmea_udp": [],
                  "notify": {"min_level": "crit", "webhook": {"url": f"http://127.0.0.1:{srv.server_port}/"}}})
    st.on_bits(synth.type5(377123456, "OCEAN SPIRIT", "J8AB2", 70, "BRIDGETOWN"), "test", "A")
    st.on_bits(synth.type1(377123456, 13.20, -59.70, 6, 10, 10), "test", "A")
    st.on_bits(synth.type1(970123456, 13.21, -59.71, 0.5, 90, 511, status=14), "test", "A")
    st.on_bits(synth.type14(970123456, "SART ACTIVE"), "test", "A")
    snap = st.snapshot()
    assert snap["alarm"] == "crit"
    crit = [a for a in snap["alerts"] if a["level"] == "crit"]
    assert crit and crit[0]["incident"] and crit[0]["mmsi"] == 970123456
    inc = Path(data) / "incidents" / crit[0]["incident"]
    report = (inc / "report.html").read_text(encoding="utf-8")
    assert "MAYDAY RELAY" in report and "OCEAN SPIRIT" in report  # nearby vessel listed
    assert "EIGHT PAPA FOXTROT X-RAY" in report
    assert (inc / "nmea.txt").read_text().count("!AIVDM") >= 1
    for _ in range(50):
        if _Hook.got:
            break
        time.sleep(0.1)
    assert _Hook.got and "EMERGENCY" in _Hook.got[0]["subject"]
    # recording made during the incident gets copied into it
    rec = Path(data) / "x_VHF_Ch16.wav"
    import wave
    with wave.open(str(rec), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\0\0" * 800)
    st._on_recording(rec, time.time() - 5, time.time(), "VHF Ch16")
    assert (inc / "recordings" / rec.name).exists()
    st.alerts.ack(None)
    assert st.snapshot()["alarm"] is None
    sid = st.incidents.snapshot(st.snapshot())
    assert (Path(data) / "snapshots" / sid / "vessels.csv").read_text().count("\n") >= 3
    assert list((Path(data) / "logs").glob("ais-*.nmea"))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
