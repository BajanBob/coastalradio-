"""Ocean City Watch engine: receivers, decoding, emergencies, incidents, alerts,
forwarding, voice recorders and station announcements."""

from __future__ import annotations

import json
import math
import random
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

import synth
from ais import NmeaAssembler, Tracker, classify, decode, to_nmea
from alerts import AlertBook
from announce import Announcer, maidenhead, script
from cat import CHANNELS, Rig, make_rig
from demod import AisDemod
from incidents import Incidents
from io_ports import AudioInput, NmeaOutput, NmeaSerialInput, NmeaUdpInput, WavInput
from notify import Notifier
from recorder import VoxRecorder, list_recordings
import composer
import dsc
import feeds
import sonde
from watch import TrafficWatch


class DscReceiver:
    """Receive-only watch on VHF Ch70 (156.525 MHz) for DSC calls."""

    def __init__(self, station: "Station", cfg: dict) -> None:
        self.station = station
        self.name = cfg.get("name", "DSC Ch70")
        fs = int(cfg.get("sample_rate", 48000))
        self.demod = dsc.DscDemod(fs, self._call)
        self.last_call = None
        self.error = None
        try:
            self.rig: Rig = make_rig(cfg)
            self.rig.tune(CHANNELS["70"])
        except Exception as e:  # noqa: BLE001
            self.rig = Rig(self.name)
            self.error = f"CAT: {e}"
        if cfg.get("wav"):
            WavInput(cfg["wav"], self.demod.process, loop=bool(cfg.get("loop")))
        elif cfg.get("audio_device") is not None:
            try:
                AudioInput(cfg["audio_device"], int(cfg.get("audio_channel", 0)), fs, self.demod.process)
            except Exception as e:  # noqa: BLE001
                self.error = f"Audio: {e}"

    def _call(self, m: dict) -> None:
        self.last_call = time.time()
        self.station.on_dsc(m, self.name)

    def status(self) -> dict:
        return {"name": self.name, "level_db": self.demod.level_db, "calls": self.demod.calls,
                "last_call": self.last_call, "rig_status": self.error or self.rig.status}


class Receiver:
    def __init__(self, station: "Station", cfg: dict, fs: int) -> None:
        self.station = station
        self.cfg = cfg
        self.name = cfg.get("name", "Receiver")
        self.mode = (cfg.get("channel") or "A").upper()  # A, B or SCAN
        self.scan_s = int(cfg.get("scan_seconds", 30))
        self.channel = "A" if self.mode == "SCAN" else self.mode
        self.last_switch = time.time()
        self.demod = AisDemod(fs, self._frame)
        self.last_frame = None
        self.error = None
        try:
            self.rig: Rig = make_rig(cfg)
        except Exception as e:  # noqa: BLE001
            self.rig = Rig(self.name)
            self.error = f"CAT: {e}"
        self.tune()

    def tune(self) -> None:
        try:
            self.rig.tune(CHANNELS[self.channel])
        except Exception as e:  # noqa: BLE001
            self.rig.status = f"CAT error: {e}"

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if mode in CHANNELS:
            self.channel = mode
            self.tune()
        self.last_switch = time.time()

    def tick(self) -> None:
        if self.mode == "SCAN" and time.time() - self.last_switch >= self.scan_s:
            self.channel = "B" if self.channel == "A" else "A"
            self.last_switch = time.time()
            self.tune()

    def feed(self, samples) -> None:
        self.demod.process(samples)

    def _frame(self, bits) -> None:
        self.last_frame = time.time()
        self.station.on_bits(bits, self.name, self.channel)

    def status(self) -> dict:
        return {"name": self.name, "rig": self.rig.kind, "rig_status": self.error or self.rig.status,
                "cat_ok": self.rig.ok and not self.error, "mode": self.mode, "channel": self.channel,
                "mhz": CHANNELS[self.channel] / 1e6, "level_db": self.demod.level_db,
                "frames": self.demod.frames, "bad": self.demod.crc_errors, "last_frame": self.last_frame}


class Station:
    def __init__(self, cfg: dict) -> None:
        st = cfg.get("station", {})
        self.cfg = cfg
        self.info = st
        self.name = st.get("name", "Coast station")
        self.callsign = st.get("callsign", "")
        self.lat, self.lon = st.get("lat"), st.get("lon")
        self.data = Path(cfg.get("data_dir", "data"))
        self.data.mkdir(parents=True, exist_ok=True)
        self.tracker = Tracker(self.lat, self.lon, int(cfg.get("vessel_timeout_min", 60)) * 60)
        log = cfg.get("log_file", str(self.data / "logs" / "ais-%Y%m%d.nmea"))
        self.out = NmeaOutput(cfg.get("nmea_udp", []), cfg.get("nmea_tcp_port"), log)
        self.asm = NmeaAssembler()
        self.notifier = Notifier(cfg.get("notify", {}), f"{self.name} {self.callsign}".strip())
        self.incidents = Incidents(self.data, st)
        self.alerts = AlertBook(self.data / "logs", self._on_alert)
        self.rec_dir = self.data / "recordings"
        self.recorders: list[VoxRecorder] = []
        ann = cfg.get("announcements", {})
        self.announcer = Announcer(self.data / "announcements", ann.get("output_device"), ann.get("reminders", []))
        self.receivers: list[Receiver] = []
        self.dsc_receivers: list[DscReceiver] = []
        self.dsc_log: deque = deque(maxlen=200)
        self.watch = TrafficWatch(cfg.get("traffic_watch", {}), self.data, self.alerts, self.lat, self.lon)
        share = cfg.get("share", [])
        self.share_targets = [f"{s['host']}:{s['port']}" for s in share if s.get("host") and s.get("port")]
        self.share_names = [s.get("name", s.get("host")) for s in share if s.get("host")]
        self.share = NmeaOutput(self.share_targets, None, None) if self.share_targets and not cfg.get("_demo") else None
        self.shared = 0
        self.chan_heard: dict[str, float] = {}
        self._last_watch_tick = 0.0
        self.inputs: list[str] = []
        self.log: deque = deque(maxlen=60)
        self.lock = threading.Lock()
        self.started = time.time()
        self.rate: deque = deque(maxlen=2000)
        # HF console, aviation and data feeds
        self.timeline: deque = deque(maxlen=600)
        self.hf_receivers: list = []
        self.sonde_receivers: list = []
        self.sondes = sonde.SondeTracker()
        self.aircraft = feeds.AircraftTracker()
        self.feed_listeners: list[feeds.FeedListener] = []
        self.feed_log: dict[str, deque] = {k: deque(maxlen=300) for k in ("hfdl", "acars", "inmarsat")}
        fc = cfg.get("feeds", {})
        self.triage = feeds.Triage(cfg.get("ai", {}), st, bool(fc.get("ai_triage", True)))

    # -------------------------------------------------- setup
    def add_audio_receiver(self, rcfg: dict) -> Receiver:
        fs = int(rcfg.get("sample_rate", 48000))
        rx = Receiver(self, rcfg, fs)
        self.receivers.append(rx)
        rx.audio = None
        if rcfg.get("wav"):
            WavInput(rcfg["wav"], rx.feed, loop=bool(rcfg.get("loop")))
        elif rcfg.get("audio_device") not in (None, ""):
            try:
                rx.audio = AudioInput(rcfg["audio_device"], int(rcfg.get("audio_channel", 0)), fs, rx.feed)
            except Exception as e:  # noqa: BLE001
                rx.error = f"Audio: {e}"
        return rx

    # -------------------------------------------------- settings page
    RX_KEYS = ("name", "enabled", "rig", "port", "baud", "civ_address", "audio_device", "audio_channel", "channel",
               "scan_seconds")
    HF_KEYS = ("name", "enabled", "rig", "model", "port", "baud", "civ_address", "audio_device", "output_device",
               "start_khz")

    def settings(self) -> dict:
        from announce import maidenhead
        from io_ports import list_audio, list_serial_ports
        st = self.cfg.get("station", {})
        hf = (self.cfg.get("hf_receivers") or [{}])[0]
        return {"station": {"name": st.get("name", ""), "callsign": st.get("callsign", ""), "lat": st.get("lat"),
                            "lon": st.get("lon"),
                            "grid": maidenhead(st["lat"], st["lon"]) if st.get("lat") is not None and st.get("lon") is not None else ""},
                "receivers": [{k: r.get(k) for k in self.RX_KEYS if k in r} for r in self.cfg.get("receivers", [])],
                "hf": {k: hf.get(k) for k in self.HF_KEYS if k in hf},
                "ports": list_serial_ports(), "audio_in": list_audio("input"), "audio_out": list_audio("output"),
                "config_path": str(Path(self.config_path).resolve()) if getattr(self, "config_path", None) else None,
                "demo": bool(self.cfg.get("_demo"))}

    @staticmethod
    def _clean_radio(r: dict, hf: bool = False) -> dict:
        from cat import ICOM_MODELS
        rig = str(r.get("rig") or "manual").lower().replace("-", "")
        if rig not in ("icom", "yaesu", "kenwood", "manual") and rig not in ICOM_MODELS:
            raise ValueError(f"Unknown radio type {rig}")
        name = str(r.get("name") or ("HF receiver" if hf else "Receiver")).strip()[:40]
        out = {"name": name, "enabled": bool(r.get("enabled", True)), "rig": rig,
               "audio_channel": int(r.get("audio_channel") or 0), "sample_rate": 48000}
        for k in ("audio_device", "output_device") if hf else ("audio_device",):
            v = r.get(k)
            out[k] = (int(v) if str(v).isdigit() else str(v)) if v not in (None, "") else None
        if not hf:
            ch = str(r.get("channel") or "A").upper()
            if ch not in ("A", "B", "SCAN"):
                raise ValueError("Channel must be A, B or SCAN")
            out["channel"] = ch
            out["scan_seconds"] = max(10, min(86400, int(r.get("scan_seconds") or 600)))
        else:
            out["model"] = str(r.get("model") or "").lower()
            out["start_khz"] = float(r.get("start_khz") or 8291)
        if rig != "manual":
            if not r.get("port"):
                raise ValueError(f"{name}: choose a COM port, or set the radio type to Manual")
            out["port"] = str(r["port"])
            out["baud"] = int(r.get("baud") or (9600 if rig in ("kenwood",) else 38400 if rig == "yaesu" else 19200))
            if rig == "icom" or rig in ICOM_MODELS:
                addr = str(r.get("civ_address") or (hex(ICOM_MODELS[rig]) if rig in ICOM_MODELS else "0x7C"))
                int(addr, 16)  # validates
                out["civ_address"] = addr
        return out

    def apply_settings(self, new: dict) -> str:
        """Check the page's settings, save them to config.json and apply them."""
        from announce import grid_to_latlon, maidenhead
        stn = new.get("station", {})
        call = "".join(ch for ch in str(stn.get("callsign") or "").upper() if ch.isalnum() or ch in "/-")[:12]
        name = str(stn.get("name") or "").strip()[:60] or "Coast station"
        grid = str(stn.get("grid") or "").strip()
        lat = float(stn["lat"]) if stn.get("lat") not in (None, "") else None
        lon = float(stn["lon"]) if stn.get("lon") not in (None, "") else None
        if (lat is None or lon is None) and grid:
            lat, lon = grid_to_latlon(grid)
        elif grid and lat is not None and maidenhead(lat, lon, len(grid))[:len(grid)].upper() != grid.upper():
            lat, lon = grid_to_latlon(grid)  # the grid was changed by hand: it wins
        if lat is None or lon is None:
            raise ValueError("Enter the station's grid square, or its latitude and longitude")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Latitude must be -90..90 and longitude -180..180")
        receivers = [self._clean_radio(r) for r in new.get("receivers", [])]
        if len({r["name"] for r in receivers}) != len(receivers):
            raise ValueError("Each AIS receiver needs a different name")
        hf = self._clean_radio(new["hf"], hf=True) if new.get("hf") else None
        if not getattr(self, "config_path", None):
            raise ValueError("Settings can't be saved in demo mode. Start the program normally to change them.")
        p = Path(self.config_path)
        try:
            saved = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            saved = {}
        saved.setdefault("station", {}).update({"name": name, "callsign": call, "lat": round(lat, 5), "lon": round(lon, 5)})
        old = {r.get("name"): r for r in saved.get("receivers", [])}
        saved["receivers"] = [{**{k: v for k, v in old.get(r["name"], {}).items() if k not in self.RX_KEYS}, **r}
                              for r in receivers]
        hf_changed = False
        if hf:
            hfl = saved.get("hf_receivers") or [{}]
            before = {k: hfl[0].get(k) for k in self.HF_KEYS}
            hfl[0] = {**hfl[0], **hf}
            hf_changed = before != {k: hfl[0].get(k) for k in self.HF_KEYS}
            saved["hf_receivers"] = hfl
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        tmp.replace(p)
        # station identity: live
        self.cfg["station"] = saved["station"]
        self.info.clear()
        self.info.update(saved["station"])
        self.name, self.callsign, self.lat, self.lon = name, call, lat, lon
        self.tracker.lat, self.tracker.lon = lat, lon
        self.watch.slat, self.watch.slon = lat, lon
        self.notifier.station = f"{name} {call}".strip()
        # AIS receivers: restart with the new settings
        for rx in self.receivers:
            try:
                if getattr(rx, "audio", None):
                    rx.audio.close()
                rx.rig.close()
            except Exception:  # noqa: BLE001
                pass
        self.receivers = []
        self.cfg["receivers"] = saved["receivers"]
        for r in saved["receivers"]:
            if r.get("enabled", True):
                self.add_audio_receiver(r)
        self.cfg["hf_receivers"] = saved.get("hf_receivers", self.cfg.get("hf_receivers"))
        msg = f"Saved to {p.name}. Station {call or name} at {maidenhead(lat, lon)} is live; AIS receivers restarted."
        if hf_changed:
            msg += " Restart the program for the HF receiver changes."
        return msg

    def test_cat(self, r: dict, hf: bool = False) -> dict:
        r = self._clean_radio(r, hf)
        if r["rig"] == "manual":
            return {"ok": True, "status": "Manual radio: nothing to test. Tune it by hand."}
        live = [x for x in self.receivers + self.hf_receivers if (x.cfg if hasattr(x, "cfg") else {}).get("port") == r["port"]]
        if live:
            return {"ok": False, "status": f"{r['port']} is in use by {live[0].name}. Save the settings to apply them."}
        rig = make_rig(r)
        try:
            ok = rig.tune(int(r["start_khz"] * 1000) if hf else CHANNELS["A"], "USB" if hf else "FM")
            return {"ok": ok, "status": rig.status}
        finally:
            rig.close()

    def add_nmea_inputs(self) -> None:
        for p in self.cfg.get("nmea_in_udp", []):
            try:
                self.inputs.append(NmeaUdpInput(int(p), self.on_nmea).name)
            except OSError as e:
                print(f"NMEA UDP {p}: {e}")
        for s in self.cfg.get("nmea_in_serial", []):
            try:
                self.inputs.append(NmeaSerialInput(s["port"], int(s.get("baud", 38400)), self.on_nmea,
                                                   s.get("name", "")).name)
            except Exception as e:  # noqa: BLE001
                print(f"NMEA serial {s.get('port')}: {e}")

    def add_dsc_receivers(self) -> None:
        for dc in self.cfg.get("dsc_receivers", []):
            if dc.get("enabled", True):
                d = DscReceiver(self, dc)
                self.dsc_receivers.append(d)
                print(f"  DSC {d.name}: {d.error or d.rig.status}")

    def on_dsc(self, m: dict, source: str) -> None:
        level, text = dsc.summary(m)
        m = {k: v for k, v in m.items() if k != "raw"}
        m.update(t=time.time(), source=source, level=level, summary=text)
        with self.lock:
            self.dsc_log.appendleft(m)
        try:
            with (self.data / "logs" / time.strftime("dsc-%Y%m%d.log", time.gmtime())).open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  {source}  {text}  {m}\n")
        except OSError:
            pass
        mmsi = m.get("distress_mmsi") or m.get("mmsi")
        if level == "crit" and mmsi:
            # put the casualty on the chart and open an incident, just like an AIS beacon
            fake = {"type": "DSC", "mmsi": mmsi, "cls": "DSC distress", "lat": m.get("lat"), "lon": m.get("lon")}
            vessel = self.tracker.update(fake, f"DSC {source}", "70")
            detail = f"{m.get('nature')} · position {m.get('lat')}, {m.get('lon')} at {m.get('time') or 'time unknown'} · " \
                     f"then {m.get('subsequent')}"
            self._emergency(fake, vessel, "crit", "DSC distress", detail)
        else:
            self.alerts.raise_(f"dsc:{m.get('mmsi')}:{m.get('format_code')}:{m.get('category_code')}:{int(time.time() // 300)}",
                               level, "DSC", text, f"{m.get('format')} · {m.get('category')} · "
                               f"{m.get('telecommand1') or ''} {('ch ' + m['channel']) if m.get('channel') else ''}".strip(),
                               mmsi=m.get("mmsi"), lat=m.get("lat"), lon=m.get("lon"))

    # -------------------------------------------------- timeline and new receivers
    def event(self, kind: str, source: str, text: str, level: str = "info", **extra) -> None:
        e = {"t": time.time(), "kind": kind, "source": source, "text": text, "level": level, **extra}
        with self.lock:
            self.timeline.appendleft(e)
        try:
            with (self.data / "logs" / time.strftime("timeline-%Y%m%d.log", time.gmtime())).open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(e['t']))}  {level.upper():4}  "
                        f"{kind:9} {source}: {text}\n")
        except OSError:
            pass

    def on_selcal(self, entry: dict, source: str) -> None:
        watch = entry.get("watch")
        text = f"SELCAL {entry['code']} on {entry['khz']:g} kHz" + (f" ({entry['where']})" if entry.get("where") else "") \
            + (f": {watch}" if watch else "")
        self.event("selcal", source, text, "warn" if watch else "info")
        if watch:
            self.alerts.raise_(f"selcal:{entry['code']}:{int(time.time() // 300)}", "warn", "SELCAL",
                               f"Watched SELCAL {entry['code']} called: {watch}", text)

    def on_sonde(self, f: dict, source: str) -> None:
        s, ev = self.sondes.update(f, source)
        if not s:
            return
        if ev == "new":
            self.event("sonde", source, f"Radiosonde {s['serial']} heard at {s['alt']} m, "
                       f"{s['lat']:.4f}, {s['lon']:.4f}")
        elif ev == "burst":
            land = s.get("landing") or {}
            self.event("sonde", source, f"Radiosonde {s['serial']} burst at {s['burst']['alt']} m; rough landing "
                       f"{land.get('lat')}, {land.get('lon')} in ~{land.get('minutes')} min", "info")
        try:
            with (self.data / "logs" / time.strftime("sondes-%Y%m%d.log", time.gmtime())).open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S', time.gmtime())} {s['serial']} fr{s.get('frame')} "
                         f"{s['lat']:.5f} {s['lon']:.5f} {s['alt']}m {s.get('climb')}m/s sats{s.get('sats')}\n")
        except OSError:
            pass

    def on_feed(self, ev: dict) -> None:
        kind = ev["feed"]
        self.feed_log[kind].appendleft(ev)
        self.aircraft.update(ev)
        try:
            with (self.data / "logs" / time.strftime(f"{kind}-%Y%m%d.log", time.gmtime())).open("a", encoding="utf-8") as fh:
                raw = ev.get("raw")
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(ev['t']))}  "
                         f"{json.dumps(raw, default=str) if raw is not None else ev.get('text')}\n")
        except OSError:
            pass
        name = {"hfdl": "HFDL", "acars": "ACARS", "inmarsat": "Inmarsat-C"}[kind]
        if ev["level"] in ("crit", "warn"):
            self.triage.submit(ev)
            self.event(kind, name, ev.get("title") or ev.get("text", "")[:160], ev["level"])
            self.alerts.raise_(f"feed:{kind}:{hash(ev.get('text') or ev.get('title'))}", ev["level"], name,
                               f"{name}: {'distress' if ev['level'] == 'crit' else 'urgency/SAR'} message",
                               (ev.get("text") or ev.get("title") or "")[:400], lat=ev.get("lat"), lon=ev.get("lon"))
        elif ev.get("cpdlc"):
            self.event(kind, name, f"CPDLC {ev.get('flight') or ev.get('reg') or ''}: {ev['cpdlc'][:140]}")

    def add_hf(self) -> None:
        from hf import HfReceiver, SondeReceiver
        for hc in self.cfg.get("hf_receivers", []):
            if hc.get("enabled", True):
                r = HfReceiver(self, hc)
                self.hf_receivers.append(r)
                print(f"  HF {r.name}: {r.error or r.rig.status}")
        for sc in self.cfg.get("radiosondes", []):
            if sc.get("enabled", True):
                r = SondeReceiver(self, sc)
                self.sonde_receivers.append(r)
                print(f"  Radiosonde {r.name}: {r.error or 'listening'}")
        fc = self.cfg.get("feeds", {})
        for kind in ("hfdl", "acars", "inmarsat"):
            c = fc.get(kind) or {}
            if c.get("enabled"):
                fl = feeds.FeedListener(kind, int(c.get("port", {"hfdl": 5555, "acars": 5556, "inmarsat": 5557}[kind])),
                                        self.on_feed, c.get("bind", "0.0.0.0"))
                self.feed_listeners.append(fl)
                print(f"  Feed {kind}: {fl.error or f'listening on UDP {fl.port}'}")

    def add_recorders(self) -> None:
        for rc in self.cfg.get("recorders", []):
            if rc.get("enabled", True):
                r = VoxRecorder(rc, self.rec_dir, self._on_recording)
                self.recorders.append(r)
                print(f"  Recorder {r.name}: {r.error or 'listening'}")

    # -------------------------------------------------- messages
    def on_bits(self, bits, source: str, channel: str) -> None:
        sentences = to_nmea(bits, channel)
        self.out.send(sentences)
        self._share(sentences)
        self._handle(bits, source, channel, sentences)

    def on_nmea(self, line: str, source: str) -> None:
        res = self.asm.feed(line)
        if not res:
            return
        bits, chan, own = res
        self.out.send([line.strip()])
        if not own:
            self._share([line.strip()])
        self._handle(bits, source + (" (own)" if own else ""), chan, [line.strip()])

    def _share(self, sentences) -> None:
        if self.share:
            self.share.send(sentences)
            self.shared += len(sentences)

    def _handle(self, bits, source, channel, sentences) -> None:
        m = decode(bits)
        if not m:
            return
        vessel = self.tracker.update(m, source, channel, sentences)
        now = time.time()
        self.rate.append(now)
        self.chan_heard[channel] = now
        try:
            self.watch.on_vessel(vessel)
        except Exception as e:  # noqa: BLE001
            print(f"[watch] {e}")
        with self.lock:
            for s in sentences:
                self.log.append({"t": now, "src": source, "type": m["type"], "mmsi": m["mmsi"], "nmea": s})
        em = classify(m)
        if em:
            self._emergency(m, vessel, *em)

    def _emergency(self, m: dict, vessel: dict, level: str, kind: str, detail: str) -> None:
        mmsi = m["mmsi"]
        name = vessel.get("name")
        lat, lon = vessel.get("lat"), vessel.get("lon")
        where = ""
        if vessel.get("range_nm") is not None:
            where = f" — {vessel['range_nm']} nm, bearing {vessel['bearing']}° from {self.callsign or 'station'}"
        title = f"{kind}: MMSI {mmsi}{' (' + name + ')' if name else ''}{where}"
        iid = None
        if level == "crit":
            self.tracker.mark(mmsi, {"level": level, "kind": kind})
            near = self.tracker.near(lat, lon, 15, mmsi) if lat is not None else []
            iid = self.incidents.update(vessel, {"level": level, "kind": kind, "detail": detail}, near)
        elif level == "warn":
            self.tracker.mark(mmsi, {"level": level, "kind": kind})
        self.alerts.raise_(f"em:{mmsi}:{kind}", level, kind, title, detail, mmsi=mmsi, lat=lat, lon=lon, incident=iid)

    def _on_alert(self, a) -> None:
        self.event("alert", a.kind, a.title, a.level)
        attach = None
        if a.incident:
            attach = self.incidents.dir / a.incident / "report.html"
        self.notifier.send_alert(a.__dict__, attach)

    def _on_recording(self, path: Path, start: float, end: float, source: str) -> None:
        self.incidents.add_recording(path, start, end, source)
        self.event("recording", source, f"Recorded {round(end - start)} s", "info",
                   file=f"{path.parent.name}/{path.name}")

    # -------------------------------------------------- periodic
    def tick(self) -> None:
        for rx in self.receivers:
            rx.tick()
        for h in self.hf_receivers:
            try:
                h.tick()
            except Exception as e:  # noqa: BLE001
                print(f"[hf] {e}")
        if time.time() - self._last_watch_tick > 15:
            self._last_watch_tick = time.time()
            try:
                self.watch.tick(self.tracker.snapshot())
            except Exception as e:  # noqa: BLE001
                print(f"[watch] {e}")
        for r in self.announcer.due_reminders():
            self.alerts.raise_(f"bcast:{r}:{time.strftime('%Y%m%d')}", "warn", "Scheduled broadcast",
                               f"Scheduled station broadcast due ({r})",
                               "Open the Announcements tab, check the channel, and play or read the announcement.")

    # -------------------------------------------------- dashboard
    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            log = list(self.log)[-40:]
        st = dict(self.info)
        if self.lat is not None:
            st["grid"] = maidenhead(self.lat, self.lon)
        return {
            "station": st,
            "receivers": [r.status() for r in self.receivers],
            "inputs": self.inputs,
            "vessels": self.tracker.snapshot(),
            "alerts": self.alerts.snapshot()[:100],
            "alarm": self.alerts.alarm(),
            "incidents": self.incidents.list(),
            "recorders": [r.status() for r in self.recorders],
            "recordings": list_recordings(self.rec_dir, 60),
            "announcements": {"files": self.announcer.list(), "playing": self.announcer.playing,
                              "last_played": self.announcer.last_played, "script": script(self.info),
                              "output_device": self.announcer.output_device,
                              "reminders": self.announcer.reminders},
            "notify": {"channels": self.notifier.channels(), "status": self.notifier.status},
            "dsc": {"receivers": [d.status() for d in self.dsc_receivers], "log": list(self.dsc_log)[:60]},
            "watch": self.watch.snapshot(),
            "share": {"targets": self.share_names, "sent": self.shared, "active": bool(self.share)},
            "channels": {c: round(now - t) for c, t in self.chan_heard.items()},
            "composer": {"templates": composer.TEMPLATES, "ai": bool(self.cfg.get("ai", {}).get("api_key")),
                         "incident": (self.incidents.active_or_latest() or {}).get("id")},
            "stats": {"messages": self.tracker.total, "per_min": sum(1 for t in self.rate if now - t < 60),
                      "max_range_nm": round(self.tracker.max_range, 1), "uptime": int(now - self.started),
                      "by_type": self.tracker.by_type},
            "outputs": {"udp": self.cfg.get("nmea_udp", []), "tcp": self.cfg.get("nmea_tcp_port")},
            "log": log, "time": now, "demo": bool(self.cfg.get("_demo")),
            "hf": [h.status() for h in self.hf_receivers],
            "sondes": {"receivers": [r.status() for r in self.sonde_receivers], "list": self.sondes.snapshot()},
            "aircraft": self.aircraft.snapshot()[:150],
            "feeds": {"listeners": [f.status() for f in self.feed_listeners],
                      "log": {k: [{k2: v for k2, v in e.items() if k2 != "raw"} for e in list(d)[:80]]
                              for k, d in self.feed_log.items()},
                      "ai": self.triage.on},
            "timeline": list(self.timeline)[:200],
        }


class DemoTraffic:
    """Simulated ships around the station, turned into audio and run through the real decoder.
    After ~25 s a simulated AIS-SART activates so the emergency features can be seen."""

    NAMES = [("OCEAN SPIRIT", "J8AB2", 70, "BRIDGETOWN"), ("CARIB TRADER", "V4XT9", 79, "PORT OF SPAIN"),
             ("SEA BREEZE", "8PXY1", 37, None), ("ISLAND PRIDE", "8PQR4", 60, "ST VINCENT"),
             ("BLUE MARLIN", "8PFS2", 30, None), ("STAR TANKER", "C6ZZ7", 80, "POINT LISAS")]

    def __init__(self, rx: Receiver, lat: float, lon: float, sart_after: float = 25) -> None:
        self.rx, self.rng = rx, np.random.default_rng()
        self.lat, self.lon = lat, lon
        self.start = time.time()
        self.sart_after = sart_after
        self.ships = []
        for i, (name, call, st, dest) in enumerate(self.NAMES):
            ang = random.uniform(0, 2 * math.pi)
            r = random.uniform(2, 18) / 60
            self.ships.append({"mmsi": (314 if st in (30, 37) else 377) * 1000000 + 100 + i,
                               "lat": lat + r * math.cos(ang), "lon": lon + r * math.sin(ang),
                               "sog": random.uniform(4, 16) if st != 30 else 3.0, "cog": random.uniform(0, 360),
                               "name": name, "call": call, "type": st, "dest": dest, "classB": st in (30, 37)})
        self.sart = {"mmsi": 970123456, "lat": lat + 0.11, "lon": lon - 0.07}
        # Traffic-watch scenario: two ships converging over a reef, a ship dragging anchor, a ship going dark.
        meet_lat, meet_lon = lat - 0.06, lon - 0.10
        self.meet = (meet_lat, meet_lon)
        self.ships += [
            {"mmsi": 377000201, "lat": meet_lat, "lon": meet_lon - 0.045, "sog": 11.0, "cog": 90.0, "name": "WINDWARD STAR",
             "call": "J8WS1", "type": 70, "dest": "BRIDGETOWN", "classB": False, "steady": True},
            {"mmsi": 377000202, "lat": meet_lat, "lon": meet_lon + 0.045, "sog": 10.0, "cog": 270.0, "name": "LEEWARD QUEEN",
             "call": "J8LQ2", "type": 60, "dest": "CASTRIES", "classB": False, "steady": True},
            {"mmsi": 377000203, "lat": lat - 0.015, "lon": lon - 0.02, "sog": 0.1, "cog": 0.0, "name": "BAY ANCHOR",
             "call": "J8BA3", "type": 80, "dest": "BRIDGETOWN", "classB": False, "steady": True, "status": 1},
            {"mmsi": 377000204, "lat": lat + 0.05, "lon": lon - 0.12, "sog": 8.0, "cog": 200.0, "name": "SILENT RUNNER",
             "call": "J8SR4", "type": 70, "dest": "", "classB": False, "steady": True, "goes_dark": 40},
        ]
        self.dsc_rx = None
        self.dsc_sent = False
        threading.Thread(target=self._run, daemon=True).start()

    def demo_zones(self) -> list[dict]:
        return [{"name": "Demo Reef", "type": "reef", "lat": self.meet[0], "lon": self.meet[1], "radius_nm": 1.2, "level": "warn"},
                {"name": "Carlisle Bay anchorage", "type": "anchorage", "lat": self.lat - 0.015, "lon": self.lon - 0.02,
                 "radius_nm": 0.8, "level": "info"}]

    def _run(self) -> None:
        n = 0
        while True:
            msgs = []
            elapsed = time.time() - self.start
            for s in self.ships:
                speedup = 6 if s.get("steady") and not s.get("status") else 1  # converge within the demo
                dt = 2 / 3600 * speedup
                s["lat"] += s["sog"] * dt / 60 * math.cos(math.radians(s["cog"]))
                s["lon"] += s["sog"] * dt / 60 * math.sin(math.radians(s["cog"])) / math.cos(math.radians(s["lat"]))
                if not s.get("steady"):
                    s["cog"] = (s["cog"] + random.uniform(-2, 2)) % 360
                if s.get("status") == 1 and elapsed > 30:
                    s["lat"] -= 0.00012  # anchor dragging ~13 m per tick
                if s.get("goes_dark") and elapsed > s["goes_dark"]:
                    continue
                if random.random() < (0.9 if s.get("steady") else 0.6):
                    f = synth.type18 if s["classB"] else synth.type1
                    if f is synth.type1:
                        msgs.append(f(s["mmsi"], s["lat"], s["lon"], s["sog"], s["cog"], int(s["cog"]), status=s.get("status", 0)))
                    else:
                        msgs.append(f(s["mmsi"], s["lat"], s["lon"], s["sog"], s["cog"], int(s["cog"])))
                if n % 10 == 0:
                    if s["classB"]:
                        msgs.append(synth.type24a(s["mmsi"], s["name"]))
                        msgs.append(synth.type24b(s["mmsi"], s["type"], s["call"]))
                    else:
                        msgs.append(synth.type5(s["mmsi"], s["name"], s["call"], s["type"], s["dest"] or ""))
            if n % 5 == 0:
                msgs.append(synth.type4(3140001, self.lat, self.lon))
            if time.time() - self.start > self.sart_after:
                s = self.sart
                s["lat"] += random.uniform(-0.0004, 0.0004)
                s["lon"] += 0.0003  # drifting
                msgs.append(synth.type1(s["mmsi"], s["lat"], s["lon"], 0.8, 90, 511, status=14))
                if n % 4 == 0:
                    msgs.append(synth.type14(s["mmsi"], "SART ACTIVE"))
            sig = np.concatenate([synth.audio(synth.frame_levels(m), noise=0.05, rng=self.rng) for m in msgs] or
                                 [np.zeros(4800)])
            for i in range(0, len(sig), 4800):
                self.rx.feed(sig[i:i + 4800])
            if self.dsc_rx and not self.dsc_sent and elapsed > 50:
                self.dsc_sent = True
                call = [112, 112] + dsc.mmsi_chars(314777888) + [105] + dsc.pos_chars(self.lat - 0.15, self.lon + 0.09) \
                    + [int(time.strftime("%H", time.gmtime())), int(time.strftime("%M", time.gmtime())), 100]
                a = dsc.fsk_audio(dsc.build_call(call)) + self.rng.normal(0, 0.05, int(len(dsc.build_call(call)) * 48000 / 1200))
                a = np.concatenate([self.rng.normal(0, 0.05, 24000), a, self.rng.normal(0, 0.05, 48000)])
                for i in range(0, len(a), 4800):
                    self.dsc_rx.demod.process(a[i:i + 4800])
            n += 1
            time.sleep(2)
