"""Tests for the HF console: DSP, SELCAL, HF DSC, radiosonde, feeds, CAT modes, station wiring.
Run:  python tests/test_hf.py"""

import json
import os
import socket
import sys
import tempfile
import time

import numpy as np
from scipy.signal import butter, lfilter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bands  # noqa: E402
import cat  # noqa: E402
import dsc  # noqa: E402
import dsp  # noqa: E402
import feeds  # noqa: E402
import selcal  # noqa: E402
import sonde  # noqa: E402

FS = 48000
RNG = np.random.default_rng(7)
_B, _A = butter(4, [300, 3000], btype="band", fs=FS)
_K = np.std(lfilter(_B, _A, RNG.normal(0, 1, FS)))


def band_noise(n, sd):
    return lfilter(_B, _A, RNG.normal(0, 1, n)) * sd / _K


def feed(dec, sig, step=4800):
    fn = getattr(dec, "process", None) or dec.feed
    for i in range(0, len(sig), step):
        fn(sig[i:i + step])


def test_selcal():
    for noise in (0.0, 0.1, 0.2):
        got = []
        d = selcal.SelcalDecoder(FS, got.append)
        sig = np.concatenate([np.zeros(FS), selcal.tone_audio("AB-CD"), np.zeros(FS // 2), selcal.tone_audio("FK-MS"), np.zeros(FS)])
        feed(d, sig + band_noise(len(sig), noise))
        assert got == ["AB-CD", "FK-MS"], (noise, got)
    got = []
    d = selcal.SelcalDecoder(FS, got.append)
    feed(d, band_noise(FS * 120, 0.2))
    assert got == [], got
    assert selcal.valid("AB-CD") and not selcal.valid("BA-CD") and not selcal.valid("AI-CD")


def test_hf_dsc():
    msg = [112, 112] + dsc.mmsi_chars(314123456) + [105] + dsc.pos_chars(13.2, -59.68) + [11, 52, 100]
    bits = [1, 0] * 100 + dsc.build_call(msg)
    got = []
    d = dsc.DscDemod(FS, got.append, **dsc.HF)
    a = dsc.fsk_audio(bits, FS, 0.3, **dsc.HF)
    sig = np.concatenate([np.zeros(FS // 2), a, np.zeros(FS // 2)])
    feed(d, sig + band_noise(len(sig), 0.15))
    assert got and got[0]["mmsi"] == 314123456 and got[0]["nature"] == "Sinking", got
    # the VHF decoder must not decode HF tones
    got2 = []
    feed(dsc.DscDemod(FS, got2.append), sig)
    assert not got2


def test_dsp_notch_nr_voice():
    t = np.arange(FS * 10) / FS
    x = band_noise(len(t), 0.1) + 0.1 * np.sin(2 * np.pi * 1180 * t)
    p = dsp.SsbDsp(FS, nr=False, notch=True, agc=False)
    y = np.concatenate([p.process(x[i:i + 4800]) for i in range(0, len(x), 4800)])
    assert any(abs(f - 1180) <= 30 for f in p.notched_hz), p.notched_hz
    from scipy.signal import welch
    f, P = welch(y[12000 * 6:], 12000, nperseg=2048)
    assert 10 * np.log10(P[np.argmin(abs(f - 1180))] / P[np.argmin(abs(f - 2000))]) < 6
    # noise reduction lowers the noise floor in silence
    x2 = band_noise(FS * 6, 0.1)
    out = {}
    for nr in (False, True):
        p = dsp.SsbDsp(FS, nr=nr, notch=False, agc=False)
        out[nr] = np.std(np.concatenate([p.process(x2[i:i + 4800]) for i in range(0, len(x2), 4800)])[12000 * 2:])
    assert out[True] < 0.75 * out[False], out
    # voice activity: none on noise, yes on a strong voice-like signal
    import demo_hf
    d = demo_hf.HfDemo.__new__(demo_hf.HfDemo)
    d.rng = RNG
    x3 = band_noise(FS * 10, 0.04)  # voice ~ +2 dB above the noise: weak but readable
    x3[FS * 4:FS * 8] += d._voice(4) * 2
    p = dsp.SsbDsp(FS)
    v = []
    for i in range(0, len(x3), 4800):
        p.process(x3[i:i + 4800])
        v.append(p.voice)
    assert not any(v[:38]) and sum(v[42:78]) > 25 and not any(v[90:]), "".join("V" if a else "." for a in v)
    # a waterfall row is produced about 5 times a second
    assert len(p.waterfall_since(0)) >= 40 and len(p.waterfall_since(0)[0][1]) == dsp.WATERFALL_BINS


def test_radiosonde():
    got = []
    d = sonde.Rs41Demod(FS, got.append)
    for inv in (1, -1):
        for i in range(3):
            fr = sonde.build_frame("T3540127", 500 + i, 13.0746, -59.4925, 12000 + 5 * i, -20, 5, 5.2)
            a = sonde.frame_audio(fr, FS) * inv
            feed(d, np.concatenate([a, np.zeros(FS // 3)]) + 0.04 + RNG.normal(0, 0.1, len(a) + FS // 3))
    assert len(got) >= 5, len(got)
    f = got[-1]
    assert f["serial"] == "T3540127" and abs(f["lat"] - 13.0746) < 1e-4 and abs(f["lon"] + 59.4925) < 1e-4
    assert abs(f["alt"] - 12010) <= 2 and abs(f["climb"] - 5.2) < 0.1 and abs(f["speed_kmh"] - 74.2) < 1
    assert abs(sonde.crc16(b"123456789") - 0x29B1) == 0  # CRC-16/CCITT-FALSE check value
    tr = sonde.SondeTracker()
    events = [tr.update({"serial": "T1", "lat": 13.1, "lon": -59.4, "alt": a, "climb": c, "speed_kmh": 40, "heading": 270})[1]
              for a, c in ((1000, 5), (25000, 5), (24000, -15))]
    assert events == ["new", None, "burst"] and tr.sondes["T1"]["landing"]["lon"] < -59.4


def test_feeds_parse():
    hf = {"hfdl": {"freq": 8927000, "sig_level": -28.1,
                   "lpdu": {"src": {"type": "Aircraft", "id": 12}, "dst": {"type": "Ground station", "name": "Shannon, Ireland"},
                            "hfnpdu": {"flight_id": "BAW2LC", "pos": {"lat": 14.1, "lon": -55.2},
                                       "acars": {"reg": ".G-XWBA", "flight": "BA2LC", "label": "AA",
                                                 "arinc622": {"cpdlc": {"elements": [{"msg_element": "CLIMB TO AND MAINTAIN FL380"}]}}}}}}}
    e = feeds.parse("hfdl", json.dumps(hf).encode())
    assert e["reg"] == "G-XWBA" and e["lat"] == 14.1 and e["freq"] == 8927.0 and "FL380" in e["cpdlc"]
    assert e["station"] == "Shannon, Ireland" and e["level"] == "info"
    e = feeds.parse("acars", json.dumps({"tail": "N1", "flight": "AA1", "text": "MAYDAY ENGINE FIRE"}).encode())
    assert e["level"] == "crit" and e["reg"] == "N1"
    e = feeds.parse("inmarsat", b"NAVAREA IV 1/26 PAN PAN VESSEL OVERDUE 13-05N 059-30W")
    assert e["level"] == "warn" and "OVERDUE" in e["text"]
    assert feeds.parse("inmarsat", b"   ") is None


def test_cat_modes_and_models():
    assert cat.IcomCIV.CIV_MODE["USB"] == 0x01 and cat.YaesuCAT.mode_cmds["USB"] == "MD02;"
    assert cat.KenwoodCAT.mode_cmds["USB"] == "MD2;" and cat.YaesuCAT.mode_cmds["FM"] == "MD04;"
    assert cat.ICOM_MODELS["ic9700"] == 0xA2 and cat.ICOM_MODELS["ic9100"] == 0x7C
    assert not cat.in_coverage("IC-9700", 161_975_000) and cat.in_coverage("ic9700", 145_000_000)
    assert cat.in_coverage("ic9100", 8_891_000) and cat.in_coverage("unknown", 1)
    assert cat.fmt_freq(8_891_000) == "8891.0 kHz" and cat.fmt_freq(161_975_000) == "161.975 MHz"


def test_bands():
    ch = bands.channels([{"khz": 6215, "label": "My net", "group": "Mine"}])
    dscs = [c for c in ch if c["kind"] == "dsc"]
    assert len(dscs) == 6 and all(abs(c["khz"] - c["dial_khz"] - 1.7) < 1e-6 for c in dscs)
    assert any(c["group"].startswith("Aviation CAR-A") and c["khz"] == 5550 for c in ch)
    assert any(c["group"] == "Mine" for c in ch) and len({c["id"] for c in ch}) == len(ch)
    assert 8291 in bands.scan_lists()["Marine distress voice"]


def test_station_hf_wiring():
    import station
    from hf import HfReceiver
    d = tempfile.mkdtemp()
    ports = {}
    for k in ("hfdl", "acars", "inmarsat"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        ports[k] = s.getsockname()[1]
        s.close()
    st = station.Station({"station": {"name": "T", "callsign": "8PFX", "lat": 13.0975, "lon": -59.6167}, "data_dir": d,
                          "nmea_udp": [], "hf_receivers": [{"name": "HF", "rig": "manual", "model": "ft991a", "start_khz": 5550,
                                                            "selcal_watch": {"AB-CD": "8P-ABC"}}],
                          "radiosondes": [{"name": "Sonde"}],
                          "feeds": {k: {"enabled": True, "port": p, "bind": "127.0.0.1"} for k, p in ports.items()}})
    st.add_hf()
    hf: HfReceiver = st.hf_receivers[0]
    feed(hf, np.concatenate([np.zeros(FS), selcal.tone_audio("AB-CD"), np.zeros(FS)]))
    assert hf.selcal_log and hf.selcal_log[0]["code"] == "AB-CD"
    assert any(a["kind"] == "SELCAL" for a in st.alerts.snapshot())
    # HF DSC through the receiver → DSC distress alert
    msg = [112, 112] + dsc.mmsi_chars(636091234) + [106] + dsc.pos_chars(14.35, -56.8) + [10, 0, 109]
    hf.tune_channel(next(c["id"] for c in hf.channels if c["kind"] == "dsc" and c["khz"] == 8414.5))
    assert abs(hf.dial_hz - 8_412_800) < 1
    feed(hf, np.concatenate([np.zeros(FS // 2), dsc.fsk_audio([1, 0] * 100 + dsc.build_call(msg), FS, 0.3, **dsc.HF), np.zeros(FS // 2)]))
    assert any(a["kind"] == "DSC distress" for a in st.alerts.snapshot()), [a["title"] for a in st.alerts.snapshot()]
    # coverage check refuses impossible frequencies
    try:
        hf.tune_khz(300000, "FM")  # 300 MHz: outside the FT-991A
        raise AssertionError("should refuse")
    except ValueError:
        pass
    # scan list steps when quiet
    hf.dwell = 0
    hf.start_scan("Marine distress voice")
    first = hf.dial_hz
    hf.tick()
    assert hf.dial_hz != first
    # sonde and feeds
    for n in range(2):  # sondes transmit every second; the first frame after start-up may be missed
        feed(st.sonde_receivers[0].demod, np.concatenate([sonde.frame_audio(sonde.build_frame("T9", n, 13.1, -59.5, 900)),
                                                          np.zeros(FS // 3)]))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps({"hfdl": {"freq": 8912000, "lpdu": {"hfnpdu": {"pos": {"lat": 15.0, "lon": -50.0},
                                                                        "acars": {"reg": ".N1", "flight": "AA1"}}}}}).encode(),
             ("127.0.0.1", ports["hfdl"]))
    s.sendto(b"DISTRESS RELAY EPIRB 13-30N 058-00W", ("127.0.0.1", ports["inmarsat"]))
    time.sleep(0.5)
    snap = st.snapshot()
    assert snap["sondes"]["list"][0]["serial"] == "T9000000"
    assert snap["aircraft"] and snap["aircraft"][0]["lat"] == 15.0
    assert any(a["kind"] == "Inmarsat-C" and a["level"] == "crit" for a in snap["alerts"])
    kinds = {e["kind"] for e in snap["timeline"]}
    assert {"selcal", "sonde", "alert", "inmarsat"} <= kinds, kinds
    assert snap["hf"][0]["scan"] == "Marine distress voice"
    json.dumps(snap, default=str)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
