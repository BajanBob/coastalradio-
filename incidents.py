"""Incident files and snapshots.

When an emergency is detected an incident folder is opened and kept up to
date: incident.json, the raw NMEA for the casualty, a printable report.html
(position, track plot, nearby vessels that could assist, a MAYDAY RELAY
draft for the operator) and copies of the voice recordings made during it.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import threading
import time
from pathlib import Path

from ais import bearing, distance_nm
from announce import dm, maidenhead, phonetic

ACTIVE_S = 2 * 3600  # an incident stays open while its beacon was heard within 2 h


def utc(t: float, fmt: str = "%Y-%m-%d %H:%M:%S UTC") -> str:
    return time.strftime(fmt, time.gmtime(t))


class Incidents:
    def __init__(self, base: Path, station: dict) -> None:
        self.dir = base / "incidents"
        self.snap_dir = base / "snapshots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.station = station
        self.active: dict[int, dict] = {}
        self.lock = threading.Lock()
        self._last_write: dict[int, float] = {}

    # -------------------------------------------------- incidents
    def update(self, vessel: dict, alert: dict, near: list[dict]) -> str:
        mmsi = vessel["mmsi"]
        now = time.time()
        with self.lock:
            inc = self.active.get(mmsi)
            if not inc or now - inc["last"] > ACTIVE_S:
                iid = f"{utc(now, '%Y%m%d-%H%M%SZ')}-{mmsi}"
                inc = {"id": iid, "mmsi": mmsi, "kind": alert["kind"], "level": alert["level"],
                       "opened": now, "last": now, "events": [], "recordings": []}
                self.active[mmsi] = inc
                (self.dir / iid).mkdir(parents=True, exist_ok=True)
            inc["last"] = now
            inc["level"] = alert["level"]
            inc["vessel"] = vessel
            inc["near"] = near
            if not inc["events"] or inc["events"][-1]["detail"] != alert["detail"]:
                inc["events"].append({"t": now, "detail": alert["detail"], "kind": alert["kind"]})
            due = now - self._last_write.get(mmsi, 0) > 20 or len(inc["events"]) == 1
        if due:
            self._last_write[mmsi] = now
            self._write(inc)
        return inc["id"]

    def add_recording(self, path: Path, start: float, end: float, source: str) -> None:
        with self.lock:
            targets = [i for i in self.active.values() if start <= i["last"] + 600 and end >= i["opened"] - 600]
        for inc in targets:
            d = self.dir / inc["id"] / "recordings"
            d.mkdir(exist_ok=True)
            try:
                shutil.copy2(path, d / path.name)
            except OSError:
                continue
            with self.lock:
                inc["recordings"].append({"file": path.name, "start": start, "end": end, "source": source})
            self._write(inc)

    def _write(self, inc: dict) -> None:
        d = self.dir / inc["id"]
        d.mkdir(parents=True, exist_ok=True)
        v = inc.get("vessel", {})
        data = {k: inc[k] for k in ("id", "mmsi", "kind", "level", "opened", "last", "events", "recordings", "near")}
        data["vessel"] = {k: val for k, val in v.items() if k != "nmea"}
        (d / "incident.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        (d / "nmea.txt").write_text("\n".join(v.get("nmea", [])) + "\n", encoding="ascii", errors="replace")
        (d / "report.html").write_text(self.report_html(inc), encoding="utf-8")

    def active_or_latest(self, iid: str | None = None) -> dict | None:
        with self.lock:
            act = sorted(self.active.values(), key=lambda i: -i["last"])
            for inc in act:
                if not iid or inc["id"] == iid:
                    return inc
        files = sorted(self.dir.glob("*/incident.json"), reverse=True)
        for p in files:
            if not iid or p.parent.name == iid:
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
        return None

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*/incident.json"), reverse=True)[:50]:
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            v = j.get("vessel", {})
            out.append({"id": j["id"], "kind": j["kind"], "level": j["level"], "mmsi": j["mmsi"],
                        "name": v.get("name"), "opened": j["opened"], "last": j["last"],
                        "active": time.time() - j["last"] < ACTIVE_S, "recordings": len(j.get("recordings", []))})
        return out

    # -------------------------------------------------- report
    def mayday_relay(self, inc: dict) -> str:
        st, v = self.station, inc.get("vessel", {})
        name = st.get("name", "COAST STATION").upper()
        call = st.get("callsign", "")
        who = f"{name} {phonetic(call)}" if call else name
        lines = ["MAYDAY RELAY, MAYDAY RELAY, MAYDAY RELAY",
                 "ALL STATIONS, ALL STATIONS, ALL STATIONS",
                 f"THIS IS {who}, {who}, {who}",
                 "MAYDAY",
                 f"{inc['kind'].upper()} RECEIVED ON AIS, MMSI {' '.join(str(inc['mmsi']))}"
                 + (f", VESSEL {v['name']}" if v.get("name") else "")]
        if v.get("lat") is not None:
            la, lo = dm(v["lat"], v["lon"])
            lines.append(f"POSITION {la} {lo}")
            if st.get("lat") is not None:
                r = distance_nm(st["lat"], st["lon"], v["lat"], v["lon"])
                b = bearing(st["lat"], st["lon"], v["lat"], v["lon"])
                lines.append(f"{r:.1f} NAUTICAL MILES, BEARING {b:03.0f} DEGREES TRUE FROM {name}")
        lines += [f"AT {utc(v.get('pos_time') or inc['last'], '%H%M')} UTC",
                  "NATURE OF DISTRESS UNKNOWN",
                  f"ALL VESSELS IN THE AREA KEEP A SHARP LOOKOUT, ASSIST IF ABLE AND REPORT TO {name}",
                  f"THIS IS {name}" + (f" {phonetic(call)}" if call else ""), "OVER"]
        return "\n".join(lines)

    def _plot(self, inc: dict) -> str:
        v, st = inc.get("vessel", {}), self.station
        pts = list(v.get("track") or [])
        if not pts and v.get("lat") is not None:
            pts = [(v["lat"], v["lon"])]
        if not pts:
            return "<p>No position received yet.</p>"
        all_pts = pts + ([(st["lat"], st["lon"])] if st.get("lat") is not None else [])
        la = [p[0] for p in all_pts]
        lo = [p[1] for p in all_pts]
        span = max(max(la) - min(la), max(lo) - min(lo), 0.01) * 1.25
        cy, cx = (max(la) + min(la)) / 2, (max(lo) + min(lo)) / 2

        def xy(lat, lon):
            return 200 + (lon - cx) / span * 380, 200 - (lat - cy) / span * 380
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(*p) for p in pts))
        x, y = xy(*pts[-1])
        svg = [f'<svg viewBox="0 0 400 400" width="400" height="400" style="background:#f4f8fb;border:1px solid #ccd">',
               f'<polyline points="{path}" fill="none" stroke="#c00" stroke-width="2"/>',
               f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="#e00"/>',
               f'<text x="{x + 10:.1f}" y="{y + 4:.1f}" font-size="12" fill="#900">{html.escape(str(v.get("name") or inc["mmsi"]))}</text>']
        if st.get("lat") is not None:
            sx, sy = xy(st["lat"], st["lon"])
            svg += [f'<rect x="{sx - 6:.1f}" y="{sy - 6:.1f}" width="12" height="12" fill="#063"/>',
                    f'<text x="{sx + 9:.1f}" y="{sy + 4:.1f}" font-size="12" fill="#063">{html.escape(st.get("callsign") or "Station")}</text>']
        svg.append(f'<text x="8" y="392" font-size="10" fill="#678">Width ≈ {span * 60:.1f} nm · north up</text></svg>')
        return "".join(svg)

    def report_html(self, inc: dict) -> str:
        v, st = inc.get("vessel", {}), self.station
        e = html.escape
        pos = "–"
        if v.get("lat") is not None:
            la, lo = dm(v["lat"], v["lon"])
            pos = f"{la} {lo} ({v['lat']:.5f}, {v['lon']:.5f}) · grid {maidenhead(v['lat'], v['lon'])}"
        rows = [("Kind", inc["kind"]), ("MMSI", inc["mmsi"]), ("Name", v.get("name")), ("Call sign", v.get("callsign")),
                ("Position", pos), ("Range / bearing from station",
                                    f"{v.get('range_nm')} nm / {v.get('bearing')}°" if v.get("range_nm") is not None else None),
                ("Speed / course", f"{v.get('sog')} kn / {v.get('cog')}°" if v.get("sog") is not None else None),
                ("Opened", utc(inc["opened"])), ("Last heard", utc(inc["last"])), ("Heard via", ", ".join(v.get("sources", [])))]
        near = "".join(f"<tr><td>{e(str(n.get('name') or n['mmsi']))}</td><td>{e(str(n.get('callsign') or ''))}</td>"
                       f"<td>{e(str(n.get('type') or ''))}</td><td>{n['range_nm']} nm</td><td>{n['bearing']}°</td>"
                       f"<td>{e(str(n.get('sog') if n.get('sog') is not None else ''))}</td></tr>" for n in inc.get("near", []))
        recs = "".join(f"<li><a href='recordings/{e(r['file'])}'>{e(r['file'])}</a> · {e(r['source'])} · "
                       f"{utc(r['start'], '%H:%M:%S')}–{utc(r['end'], '%H:%M:%S')} UTC</li>" for r in inc.get("recordings", []))
        ev = "".join(f"<li>{utc(x['t'])} — {e(x['kind'])}: {e(x['detail'])}</li>" for x in inc.get("events", []))
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Incident {e(inc['id'])}</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#123;max-width:980px}}h1{{font-size:20px;margin:0}}
.crit{{color:#b00}}table{{border-collapse:collapse;margin:8px 0}}td,th{{border:1px solid #ccd;padding:4px 8px;text-align:left}}
pre{{background:#fff4f4;border:2px solid #c00;padding:12px;font-size:15px;white-space:pre-wrap}}
.grid{{display:flex;gap:24px;flex-wrap:wrap}}small{{color:#567}}</style></head><body>
<h1 class="{'crit' if inc['level'] == 'crit' else ''}">{e(inc['kind'])} — MMSI {inc['mmsi']}</h1>
<small>{e(st.get('name', ''))} {e(st.get('callsign', ''))} · incident {e(inc['id'])} · report updated {utc(time.time())}</small>
<div class="grid"><div><table>{''.join(f'<tr><th>{k}</th><td>{e(str(val))}</td></tr>' for k, val in rows if val)}</table></div>
<div>{self._plot(inc)}</div></div>
<h2>Vessels nearby that may be able to assist</h2>
{f'<table><tr><th>Vessel</th><th>Call sign</th><th>Type</th><th>Distance</th><th>Bearing from casualty</th><th>Speed</th></tr>{near}</table>' if near else '<p>None with a known position within 15 nm.</p>'}
<h2>Draft MAYDAY RELAY (for the operator to read on voice)</h2>
<p><small>Use only in line with your MRCC's instructions and national procedures. Check every detail before transmitting.</small></p>
<pre>{e(self.mayday_relay(inc))}</pre>
<h2>Timeline</h2><ul>{ev}</ul>
<h2>Voice recordings</h2>{f'<ul>{recs}</ul>' if recs else '<p>No recordings linked yet.</p>'}
<p><small>Raw AIS data: <a href="nmea.txt">nmea.txt</a> · machine-readable: <a href="incident.json">incident.json</a></small></p>
</body></html>"""

    # -------------------------------------------------- snapshots
    def snapshot(self, state: dict) -> str:
        now = time.time()
        sid = utc(now, "%Y%m%d-%H%M%SZ")
        d = self.snap_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        e = html.escape
        rows = "".join(
            f"<tr><td>{e(str(v.get('name') or ''))}</td><td>{v['mmsi']}</td><td>{e(str(v.get('callsign') or ''))}</td>"
            f"<td>{e(str(v.get('shiptype_name') or v.get('cls') or ''))}</td>"
            f"<td>{'%.5f, %.5f' % (v['lat'], v['lon']) if v.get('lat') is not None else ''}</td>"
            f"<td>{v.get('sog') if v.get('sog') is not None else ''}</td><td>{v.get('cog') if v.get('cog') is not None else ''}</td>"
            f"<td>{v.get('range_nm') if v.get('range_nm') is not None else ''}</td><td>{e(str(v.get('destination') or ''))}</td>"
            f"<td>{utc(v['last'], '%H:%M:%S')}</td></tr>" for v in state.get("vessels", []))
        csv = ["mmsi,name,callsign,type,lat,lon,sog,cog,range_nm,destination,last_utc"]
        for v in state.get("vessels", []):
            vals = [v["mmsi"], v.get("name"), v.get("callsign"), v.get("shiptype_name") or v.get("cls"), v.get("lat"),
                    v.get("lon"), v.get("sog"), v.get("cog"), v.get("range_nm"), v.get("destination"), utc(v["last"])]
            csv.append(",".join('"' + re.sub('"', "'", str(x)) + '"' if x is not None else "" for x in vals))
        (d / "vessels.csv").write_text("\n".join(csv) + "\n", encoding="utf-8")
        (d / "snapshot.html").write_text(
            f"<!doctype html><meta charset='utf-8'><title>Snapshot {sid}</title>"
            f"<style>body{{font:13px system-ui;margin:20px}}td,th{{border:1px solid #ccd;padding:3px 6px}}table{{border-collapse:collapse}}</style>"
            f"<h2>{e(self.station.get('name', ''))} {e(self.station.get('callsign', ''))} — AIS snapshot {utc(now)}</h2>"
            f"<p>{len(state.get('vessels', []))} vessels · {state.get('stats', {}).get('messages', 0)} messages since start</p>"
            f"<table><tr><th>Name</th><th>MMSI</th><th>Call</th><th>Type</th><th>Position</th><th>Kn</th><th>COG</th>"
            f"<th>Range nm</th><th>Destination</th><th>Last (UTC)</th></tr>{rows}</table>", encoding="utf-8")
        return sid
