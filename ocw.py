#!/usr/bin/env python3
"""Ocean City Watch: receive-only AIS and SAR watch-keeping for a coast station.

    python ocw.py --list-audio          show sound inputs / outputs
    python ocw.py                       run with config.json
    python ocw.py --demo                simulated ships (and a simulated AIS-SART) through the real decoder
    python ocw.py --wav recording.wav   decode a recording of a radio's 9600 audio

Radios are only tuned (frequency + FM mode). Ocean City Watch has no
transmit or PTT function; announcements are played only when the operator
presses Play, and the operator keys the radio.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent


def load_config(path: str) -> tuple[dict, bool]:
    """Returns (config, created_now)."""
    p = Path(path)
    if not p.exists():
        example = HERE / "config.example.json"
        if not example.exists():
            example = APP_DIR / "config.example.json"
        if example.exists() and path == "config.json":
            shutil.copy(example, p)
            return json.loads(p.read_text(encoding="utf-8")), True
        raise FileNotFoundError(f"Config file {p.resolve()} not found.")
    try:
        return json.loads(p.read_text(encoding="utf-8")), False
    except ValueError as e:
        raise ValueError(f"config.json has a typing mistake (line {getattr(e, 'lineno', '?')}): {e}. "
                         "Check for a missing comma or quote, or delete config.json to start fresh.") from e


def selftest() -> int:
    """Checks every part of the installed program (run by the build server on the finished exe)."""
    import numpy as np
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            res = fn()
            print(f"{name}: OK{' (' + str(res) + ')' if res not in (None, True) else ''}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"{name}: FAILED ({type(e).__name__}: {e})")

    def ais():
        import synth
        from ais import decode
        from demod import AisDemod
        got = []
        d = AisDemod(48000, got.append)
        sig = synth.audio(synth.frame_levels(synth.type1(377123456, 13.1, -59.6, 10.0, 90.0, 90)), noise=0.03,
                          rng=np.random.default_rng(1))
        for i in range(0, len(sig), 4800):
            d.process(sig[i:i + 4800])
        assert len(got) == 1 and decode(got[0])["mmsi"] == 377123456

    def vhf_dsc():
        import dsc
        got = []
        d = dsc.DscDemod(48000, got.append)
        msg = [112, 112] + dsc.mmsi_chars(314123456) + [105] + dsc.pos_chars(13.2, -59.68) + [11, 52, 100]
        d.process(np.concatenate([np.zeros(4800), dsc.fsk_audio(dsc.build_call(msg)), np.zeros(4800)]))
        assert got and got[0]["mmsi"] == 314123456

    def hf_dsc_selcal():
        import dsc
        import selcal
        got = []
        d = selcal.SelcalDecoder(48000, got.append)
        d.process(np.concatenate([np.zeros(24000), selcal.tone_audio("AB-CD"), np.zeros(24000)]))
        assert got == ["AB-CD"]
        got2 = []
        h = dsc.DscDemod(48000, got2.append, **dsc.HF)
        msg = [112, 112] + dsc.mmsi_chars(314123456) + [105] + dsc.pos_chars(13.2, -59.68) + [11, 52, 100]
        a = np.concatenate([np.zeros(4800), dsc.fsk_audio([1, 0] * 50 + dsc.build_call(msg), 48000, 0.3, **dsc.HF),
                            np.zeros(24000)])
        for i in range(0, len(a), 4800):
            h.process(a[i:i + 4800])
        assert got2

    def radiosonde():
        import sonde
        got = []
        d = sonde.Rs41Demod(48000, got.append)
        for n in range(2):
            d.process(np.concatenate([sonde.frame_audio(sonde.build_frame("T1234567", n, 13.07, -59.49, 1500)),
                                      np.zeros(16000)]))
        assert got and got[-1]["serial"] == "T1234567"

    def dsp_chain():
        import dsp
        p = dsp.SsbDsp(48000)
        y = p.process(np.random.default_rng(1).normal(0, 0.05, 48000))
        assert len(y) > 10000

    def modules():
        import bands, cat, composer, feeds, hf, incidents, notify, recorder, server, station, watch, demo_hf  # noqa: F401,E401
        return f"{len(bands.channels())} HF channels"

    def web():
        assert (HERE / "web" / "index.html").exists(), "web/index.html missing"

    def example_config():
        p = HERE / "config.example.json"
        json.loads(p.read_text(encoding="utf-8"))

    def audio():
        try:
            import sounddevice  # noqa: F401
            return "sound system ready"
        except Exception as e:  # noqa: BLE001 - a build server has no sound card
            return f"library present, no sound system here ({type(e).__name__})"

    def serial_lib():
        import serial  # noqa: F401

    for name, fn in (("Modules", modules), ("Dashboard page", web), ("Example config", example_config),
                     ("AIS decoder", ais), ("VHF DSC decoder", vhf_dsc), ("SELCAL + HF DSC", hf_dsc_selcal),
                     ("Radiosonde decoder", radiosonde), ("HF processing", dsp_chain), ("Serial/CAT", serial_lib),
                     ("Audio", audio)):
        check(name, fn)
    print("SELF-TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def list_devices() -> str:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:
        return f"Audio unavailable ({e}). Install it with: pip install sounddevice"
    lines = ["Audio devices (use the number or name in config.json):"]
    for i, d in enumerate(sd.query_devices()):
        io = []
        if d["max_input_channels"]:
            io.append(f"in {d['max_input_channels']}ch")
        if d["max_output_channels"]:
            io.append(f"out {d['max_output_channels']}ch")
        lines.append(f"  [{i}] {d['name']}  ({', '.join(io)}, {int(d['default_samplerate'])} Hz)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ocean City Watch: AIS & SAR watch for coast stations")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--list-audio", action="store_true", help="List audio devices and exit")
    ap.add_argument("--demo", action="store_true", help="Simulated ships and a simulated AIS-SART")
    ap.add_argument("--wav", help="Decode a .wav recording instead of live audio")
    ap.add_argument("--port", type=int, help="Dashboard port (default 8100)")
    ap.add_argument("--lan", action="store_true", help="Let other computers on your network open the dashboard")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="Check the installation and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.list_audio:
        print(list_devices())
        return 0

    from server import serve
    from station import DemoTraffic, Station

    if not args.demo and not args.wav:
        cfg, created = load_config(args.config)
        if created:
            print(f"\n  First run: created {Path(args.config).resolve()}")
            print("  Open it in Notepad and set your radios' COM ports, audio devices and alerts,")
            print("  then start the program again. Starting the DEMO now so you can see how it works.\n")
            args.demo = True
    if args.demo:
        cfg = {"station": {"name": "OCEAN CITY RADIO (DEMO)", "callsign": "8PFX", "lat": 13.0975, "lon": -59.6167},
               "data_dir": tempfile.mkdtemp(prefix="ocw-demo-"), "nmea_udp": [], "_demo": True,
               "announcements": {"reminders": []},
               "traffic_watch": {"dark_minutes_a": 0.5, "dark_range_nm": 30},
               "hf_receivers": [{"name": "FT-991A HF (demo)", "rig": "manual", "model": "ft991a",
                                 "start_khz": 5550, "selcal_watch": {"AB-CD": "Demo: BWA 8P-ABC (watched)"}}],
               "radiosondes": [{"name": "RTL-SDR 403 MHz (demo)", "frequency_mhz": 403.0}],
               "feeds": {k: {"enabled": True, "port": p, "bind": "127.0.0.1"}
                         for k, p in (("hfdl", 15555), ("acars", 15556), ("inmarsat", 15557))}}
    elif args.wav:
        cfg = {"station": {"name": "WAV FILE"}, "nmea_udp": [], "data_dir": "data"}
    st = Station(cfg)

    if args.demo:
        from station import DscReceiver
        rx = st.add_audio_receiver({"name": "Demo receiver", "rig": "manual", "channel": "A"})
        demo = DemoTraffic(rx, cfg["station"]["lat"], cfg["station"]["lon"])
        for z in demo.demo_zones():
            st.watch.add_zone(z)
        st.watch.add_watch(377000105, "Tanker of interest", "Demo watchlist entry", "info")
        d = DscReceiver(st, {"name": "Demo DSC Ch70", "rig": "manual"})
        st.dsc_receivers.append(d)
        demo.dsc_rx = d
        st.add_hf()
        import demo_hf
        demo_hf.HfDemo(st.hf_receivers[0])
        demo_hf.SondeDemo(st.sonde_receivers[0])
        demo_hf.FeedDemo({f.kind: f.port for f in st.feed_listeners})
    elif args.wav:
        st.add_audio_receiver({"name": Path(args.wav).name, "rig": "manual", "wav": args.wav})
    else:
        for rcfg in cfg.get("receivers", []):
            if rcfg.get("enabled", True):
                rx = st.add_audio_receiver(rcfg)
                print(f"  {rx.name}: {rx.error or rx.rig.status}")
        st.add_nmea_inputs()
        st.add_dsc_receivers()
        st.add_recorders()
        st.add_hf()
        if st.notifier.channels():
            print(f"  Alert forwarding: {', '.join(st.notifier.channels())}")

    def ticker():
        while True:
            st.tick()
            time.sleep(1)
    threading.Thread(target=ticker, daemon=True).start()

    port = args.port or int(cfg.get("web_port", 8100))
    httpd = None
    for p in range(port, port + 10):  # another program (e.g. AIS Station) may already use the port
        try:
            httpd = serve(st, p, lan=args.lan or bool(cfg.get("web_lan")))
            if p != port:
                print(f"  Port {port} is busy (is AIS Station or another copy running?), using {p} instead.")
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print(f"  Ports {port}-{port + 9} are all busy. Close other copies of the program and try again.")
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  OCEAN CITY WATCH running{' (DEMO)' if args.demo else ''}. Dashboard: {url}")
    print(f"  Data folder: {Path(cfg.get('data_dir', 'data')).resolve()}")
    print("  Receive-only: no transmit function. Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    return 0


def run() -> int:
    """Run main(); if anything goes wrong, show the error and keep the window open."""
    if FROZEN:
        import os
        os.chdir(APP_DIR)  # keep config.json, data and logs next to the program, however it was started
    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (print(e.code) or 1)
    except KeyboardInterrupt:
        code = 0
    except Exception as e:  # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        print(f"\n  The program stopped because of an error:\n  {e}\n")
        try:
            (APP_DIR / "crash.log").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n") + detail, encoding="utf-8")
            print(f"  Details were saved to {APP_DIR / 'crash.log'}. Send that file for help.")
        except OSError:
            print(detail)
        code = 1
    if FROZEN and code not in (0, None) and "--selftest" not in sys.argv:
        try:
            input("\n  Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code or 0


if __name__ == "__main__":
    sys.exit(run())
