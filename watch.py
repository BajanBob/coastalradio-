"""Traffic watch: geofences, collision risk (CPA/TCPA), AIS gaps ("dark" vessels),
anchor-drag watch, a watchlist, and the reception coverage map."""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

from ais import bearing, distance_nm

ZONE_TYPES = ("reef", "restricted", "harbour", "anchorage", "marine park", "custom")


def point_in_polygon(lat: float, lon: float, poly: list[list[float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def in_zone(z: dict, lat: float, lon: float) -> bool:
    if z.get("polygon"):
        return point_in_polygon(lat, lon, z["polygon"])
    if z.get("lat") is not None and z.get("radius_nm"):
        return distance_nm(lat, lon, z["lat"], z["lon"]) <= z["radius_nm"]
    return False


def cpa_tcpa(a: dict, b: dict) -> tuple[float, float]:
    """Closest point of approach (nm) and time to it (minutes) on a local flat plane."""
    lat0 = math.radians((a["lat"] + b["lat"]) / 2)
    ax, ay = a["lon"] * 60 * math.cos(lat0), a["lat"] * 60
    bx, by = b["lon"] * 60 * math.cos(lat0), b["lat"] * 60
    avx, avy = a["sog"] * math.sin(math.radians(a["cog"])), a["sog"] * math.cos(math.radians(a["cog"]))
    bvx, bvy = b["sog"] * math.sin(math.radians(b["cog"])), b["sog"] * math.cos(math.radians(b["cog"]))
    dx, dy, dvx, dvy = bx - ax, by - ay, bvx - avx, bvy - avy
    dv2 = dvx * dvx + dvy * dvy
    if dv2 < 1e-9:
        return math.hypot(dx, dy), float("inf")
    t = -(dx * dvx + dy * dvy) / dv2  # hours
    return math.hypot(dx + dvx * t, dy + dvy * t), t * 60


class TrafficWatch:
    def __init__(self, cfg: dict, data: Path, alerts, station_lat, station_lon) -> None:
        self.cfg = cfg or {}
        self.data = data
        self.alerts = alerts
        self.slat, self.slon = station_lat, station_lon
        self.lock = threading.Lock()
        self.zones_file = data / "zones.json"
        self.watch_file = data / "watchlist.json"
        self.cov_file = data / "coverage.json"
        self.zones: list[dict] = self._load(self.zones_file, None) or list(self.cfg.get("zones", []))
        self.watchlist: dict[str, dict] = self._load(self.watch_file, {})
        cov = self._load(self.cov_file, {})
        self.sectors: list[float] = cov.get("sectors", [0.0] * 36)
        self.cells: dict[str, int] = cov.get("cells", {})
        self.inside: dict[int, set] = {}
        self.anchors: dict[int, dict] = {}
        self.dark: dict[int, dict] = {}
        self.risks: list[dict] = []
        self.watch_seen: dict[int, float] = {}
        self._cov_saved = time.time()

    @staticmethod
    def _load(p: Path, default):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self, p: Path, obj) -> None:
        p.write_text(json.dumps(obj, indent=1), encoding="utf-8")

    # ------------------------------------------------ settings (from dashboard)
    def add_zone(self, z: dict) -> None:
        name = str(z.get("name", "")).strip()[:40]
        if not name:
            raise ValueError("Zone needs a name")
        ztype = z.get("type") if z.get("type") in ZONE_TYPES else "custom"
        zone = {"name": name, "type": ztype, "level": "warn" if z.get("level") != "info" else "info"}
        if z.get("polygon"):
            zone["polygon"] = [[float(a), float(b)] for a, b in z["polygon"]]
        else:
            zone.update(lat=float(z["lat"]), lon=float(z["lon"]), radius_nm=float(z["radius_nm"]))
        with self.lock:
            self.zones = [x for x in self.zones if x["name"] != name] + [zone]
            self._save(self.zones_file, self.zones)

    def remove_zone(self, name: str) -> None:
        with self.lock:
            self.zones = [x for x in self.zones if x["name"] != name]
            self._save(self.zones_file, self.zones)

    def add_watch(self, mmsi: int, label: str, note: str = "", level: str = "info") -> None:
        with self.lock:
            self.watchlist[str(int(mmsi))] = {"label": label[:60], "note": note[:200],
                                             "level": level if level in ("info", "warn", "crit") else "info"}
            self._save(self.watch_file, self.watchlist)

    def remove_watch(self, mmsi: int) -> None:
        with self.lock:
            self.watchlist.pop(str(int(mmsi)), None)
            self._save(self.watch_file, self.watchlist)

    # ------------------------------------------------ per message
    def on_vessel(self, v: dict) -> None:
        mmsi = v["mmsi"]
        w = self.watchlist.get(str(mmsi))
        if w:
            last = self.watch_seen.get(mmsi, 0)
            if time.time() - last > 1800:
                self.alerts.raise_(f"watch:{mmsi}:{int(time.time() // 1800)}", w["level"], "Watchlist",
                                   f"Watchlist: {w['label']} ({v.get('name') or mmsi}) is in range",
                                   w.get("note", ""), mmsi=mmsi, lat=v.get("lat"), lon=v.get("lon"))
            self.watch_seen[mmsi] = time.time()
        if v.get("lat") is None or v.get("cls") in ("AtoN", "Base station"):
            return
        lat, lon = v["lat"], v["lon"]
        self._coverage(lat, lon)
        self._zones(v, lat, lon)
        self._anchor(v, lat, lon)
        if mmsi in self.dark:
            gap = self.dark.pop(mmsi)
            self.alerts.raise_(f"darkback:{mmsi}:{int(gap['since'])}", "info", "AIS gap",
                               f"{v.get('name') or mmsi} transmitting again after {int((time.time() - gap['since']) / 60)} min silence",
                               f"Reappeared {distance_nm(gap['lat'], gap['lon'], lat, lon):.1f} nm from where it went silent.",
                               mmsi=mmsi, lat=lat, lon=lon)

    def _coverage(self, lat: float, lon: float) -> None:
        if self.slat is None:
            return
        d = distance_nm(self.slat, self.slon, lat, lon)
        if d > 300:
            return
        s = int(bearing(self.slat, self.slon, lat, lon) // 10) % 36
        key = f"{math.floor(lat * 20) / 20:.2f},{math.floor(lon * 20) / 20:.2f}"
        with self.lock:
            self.sectors[s] = max(self.sectors[s], round(d, 1))
            self.cells[key] = self.cells.get(key, 0) + 1
        if time.time() - self._cov_saved > 120:
            self._cov_saved = time.time()
            with self.lock:
                self._save(self.cov_file, {"sectors": self.sectors, "cells": self.cells})

    def _zones(self, v: dict, lat: float, lon: float) -> None:
        mmsi = v["mmsi"]
        now_in = {z["name"] for z in self.zones if in_zone(z, lat, lon)}
        before = self.inside.get(mmsi, set())
        for name in now_in - before:
            z = next(z for z in self.zones if z["name"] == name)
            self.alerts.raise_(f"zone:{name}:{mmsi}", z.get("level", "warn"), "Geofence",
                               f"{v.get('name') or mmsi} entered {z['type']} zone '{name}'",
                               f"{v.get('shiptype_name') or v.get('cls') or ''} · {v.get('sog', '?')} kn · {v.get('status') or ''}".strip(" ·"),
                               mmsi=mmsi, lat=lat, lon=lon)
        self.inside[mmsi] = now_in

    def _anchor(self, v: dict, lat: float, lon: float) -> None:
        mmsi = v["mmsi"]
        anchored = v.get("status") == "At anchor" or (
            any(z["type"] == "anchorage" and z["name"] in self.inside.get(mmsi, set()) for z in self.zones)
            and (v.get("sog") or 0) < 0.5)
        a = self.anchors.get(mmsi)
        if not anchored:
            if a and (v.get("sog") or 0) > 3:
                self.anchors.pop(mmsi, None)  # got under way deliberately
            return
        if not a:
            self.anchors[mmsi] = {"lat": lat, "lon": lon, "since": time.time(), "name": v.get("name"), "max_m": 0}
            return
        moved_m = distance_nm(a["lat"], a["lon"], lat, lon) * 1852
        a["max_m"] = max(a["max_m"], round(moved_m))
        a["name"] = v.get("name") or a["name"]
        limit = float(self.cfg.get("anchor_radius_m", 150))
        if moved_m > limit and (v.get("sog") or 0) < 3:
            self.alerts.raise_(f"drag:{mmsi}:{int(a['since'])}", "warn", "Anchor watch",
                               f"{v.get('name') or mmsi} may be dragging anchor",
                               f"Moved {moved_m:.0f} m from its anchor position (limit {limit:.0f} m), now {v.get('sog', 0)} kn.",
                               mmsi=mmsi, lat=lat, lon=lon)

    # ------------------------------------------------ periodic (every ~15 s)
    def tick(self, vessels: list[dict]) -> None:
        now = time.time()
        self._cpa(vessels, now)
        self._dark(vessels, now)

    def _cpa(self, vessels, now) -> None:
        rng = float(self.cfg.get("cpa_range_nm", 25))
        lim_nm = float(self.cfg.get("cpa_nm", 0.5))
        lim_min = float(self.cfg.get("tcpa_min", 15))
        movers = [v for v in vessels if v.get("lat") is not None and (v.get("sog") or 0) >= 2 and v.get("cog") is not None
                  and now - v.get("pos_time", 0) < 180 and v.get("cls") in ("A", "B")
                  and (v.get("range_nm") is None or v["range_nm"] <= rng)]
        risks = []
        for i in range(len(movers)):
            for j in range(i + 1, len(movers)):
                a, b = movers[i], movers[j]
                if distance_nm(a["lat"], a["lon"], b["lat"], b["lon"]) > 12:
                    continue
                cpa, tcpa = cpa_tcpa(a, b)
                if cpa < lim_nm and 0 < tcpa < lim_min:
                    pair = sorted([a["mmsi"], b["mmsi"]])
                    names = f"{a.get('name') or a['mmsi']} and {b.get('name') or b['mmsi']}"
                    risks.append({"a": a["mmsi"], "b": b["mmsi"], "names": names, "cpa_nm": round(cpa, 2),
                                  "tcpa_min": round(tcpa, 1), "lat": (a["lat"] + b["lat"]) / 2, "lon": (a["lon"] + b["lon"]) / 2})
                    self.alerts.raise_(f"cpa:{pair[0]}:{pair[1]}", "warn", "Collision risk",
                                       f"Collision risk: {names}",
                                       f"Closest approach {cpa:.2f} nm in {tcpa:.0f} min if both hold course and speed.",
                                       lat=(a["lat"] + b["lat"]) / 2, lon=(a["lon"] + b["lon"]) / 2)
        self.risks = risks

    def _dark(self, vessels, now) -> None:
        rng = float(self.cfg.get("dark_range_nm", 15))
        for v in vessels:
            mmsi = v["mmsi"]
            if mmsi in self.dark or v.get("cls") not in ("A", "B") or v.get("count", 0) < 5:
                continue
            if (v.get("sog") or 0) < 1 or v.get("range_nm") is None or v["range_nm"] > rng:
                continue
            limit = float(self.cfg.get("dark_minutes_a" if v["cls"] == "A" else "dark_minutes_b", 10 if v["cls"] == "A" else 15))
            silent = (now - v["last"]) / 60
            if silent > limit:
                self.dark[mmsi] = {"since": v["last"], "lat": v["lat"], "lon": v["lon"], "name": v.get("name")}
                self.alerts.raise_(f"dark:{mmsi}:{int(v['last'])}", "warn", "AIS gap",
                                   f"{v.get('name') or mmsi} stopped transmitting AIS",
                                   f"Last heard {silent:.0f} min ago, {v['range_nm']} nm out, moving at {v.get('sog')} kn "
                                   f"on {v.get('cog')}°. AIS may have been switched off, or the vessel is in trouble.",
                                   mmsi=mmsi, lat=v["lat"], lon=v["lon"])

    # ------------------------------------------------ dashboard
    def snapshot(self) -> dict:
        with self.lock:
            cells = sorted(self.cells.items(), key=lambda kv: -kv[1])[:3000]
            return {"zones": self.zones, "watchlist": self.watchlist, "risks": self.risks,
                    "dark": [{"mmsi": k, **v} for k, v in self.dark.items()],
                    "anchors": [{"mmsi": k, **v} for k, v in self.anchors.items()],
                    "coverage": {"sectors": self.sectors, "cells": cells}}
