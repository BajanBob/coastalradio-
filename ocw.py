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


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        example = HERE / "config.example.json"
        if example.exists() and path == "config.json":
            shutil.copy(example, p)
            print(f"Created {p} from the example. Edit it for your radios, station and alerts, then restart.")
        else:
            sys.exit(f"Config file {p} not found.")
    return json.loads(p.read_text(encoding="utf-8"))


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
    args = ap.parse_args()

    if args.list_audio:
        print(list_devices())
        return 0

    from server import serve
    from station import DemoTraffic, Station

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
    else:
        cfg = load_config(args.config)
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
    try:
        httpd = serve(st, port, lan=args.lan or bool(cfg.get("web_lan")))
    except OSError:
        print(f"Port {port} is busy. Try --port {port + 1}")
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


if __name__ == "__main__":
    sys.exit(main())
