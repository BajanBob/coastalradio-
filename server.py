"""Local web server for the Ocean City Watch dashboard (127.0.0.1 only unless --lan)."""

from __future__ import annotations

import json
import mimetypes
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "web"
AREAS = ("recordings", "incidents", "snapshots", "announcements")


class Handler(BaseHTTPRequestHandler):
    station = None
    allow_lan = False

    def log_message(self, *_a) -> None:
        pass

    def _host_ok(self) -> bool:
        if self.allow_lan:
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode())

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, b"{}")
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(self.station.snapshot())
        if path.startswith("/api/hf/"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            rx = self._hf(q.get("rx", [""])[0])
            if not rx:
                return self._send(404, b"{}")
            if path == "/api/hf/catalog":
                return self._json(rx.catalog())
            if path == "/api/hf/waterfall":
                return self._json(rx.dsp.waterfall_since(int(q.get("since", ["0"])[0] or 0)))
        if path.startswith("/files/"):
            return self._file(urllib.parse.unquote(path[len("/files/"):]))
        self._send(404, b"{}")

    def _hf(self, name: str):
        hfs = self.station.hf_receivers
        return next((h for h in hfs if h.name == name), hfs[0] if hfs and not name else None)

    def _file(self, rel: str) -> None:
        area, _, sub = rel.partition("/")
        if area not in AREAS or not sub:
            return self._send(404, b"{}")
        base = (self.station.data / area).resolve()
        target = (base / sub).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self._send(404, b"{}")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype == "text/html":
            ctype = "text/html; charset=utf-8"
        # Reports are our own files, but keep them sandboxed from the dashboard anyway.
        extra = {"Content-Security-Policy": "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"}
        self._send(200, target.read_bytes(), ctype, extra)

    def do_POST(self) -> None:
        if not self._host_ok() or self.headers.get("X-OCW") != "1":
            return self._send(403, b"{}")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 26_000_000:
            return self._json({"ok": False, "error": "Too large"}, 413)
        raw = self.rfile.read(n) if n else b""
        st = self.station
        try:
            if self.path == "/api/announce/upload":
                name = st.announcer.save(urllib.parse.unquote(self.headers.get("X-Name", "announcement")), raw)
                return self._json({"ok": True, "name": name})
            data = json.loads(raw or b"{}")
            if self.path == "/api/channel":
                mode = str(data.get("mode", "")).upper()
                if mode not in ("A", "B", "SCAN"):
                    raise ValueError("mode must be A, B or SCAN")
                for rx in st.receivers:
                    if rx.name == data.get("rx"):
                        rx.set_mode(mode)
            elif self.path.startswith("/api/hf/"):
                rx = self._hf(str(data.get("rx", "")))
                if not rx:
                    raise ValueError("No HF receiver configured")
                if self.path == "/api/hf/tune":
                    if data.get("channel"):
                        status = rx.tune_channel(str(data["channel"]))
                    else:
                        rx.stop_scan()
                        status = rx.tune_khz(float(data["khz"]), str(data.get("mode", "USB")),
                                             float(data.get("offset_khz", 0) or 0))
                    return self._json({"ok": True, "status": status})
                if self.path == "/api/hf/scan":
                    rx.stop_scan() if data.get("stop") else rx.start_scan(str(data["list"]))
                elif self.path == "/api/hf/dsp":
                    rx.set_dsp(**{k: v for k, v in data.items() if k != "rx"})
                else:
                    return self._send(404, b"{}")
            elif self.path == "/api/feed/ai":
                ev = next((e for e in st.feed_log.get(str(data.get("kind")), []) if abs(e["t"] - float(data["t"])) < 1e-3), None)
                if not ev:
                    raise ValueError("message not found")
                if not st.triage.cfg.get("api_key"):
                    raise ValueError("Add your Anthropic API key under \"ai\" in config.json to use AI explanations")
                ev["ai"] = st.triage.explain(ev)
                return self._json({"ok": True, "text": ev["ai"]})
            elif self.path == "/api/ack":
                st.alerts.ack(data.get("key") or None, data.get("who") or "operator")
            elif self.path == "/api/snapshot":
                return self._json({"ok": True, "id": st.incidents.snapshot(st.snapshot())})
            elif self.path == "/api/record":
                for r in st.recorders:
                    if r.name == data.get("name"):
                        r.record_now(float(data.get("minutes", 5)))
            elif self.path == "/api/announce/play":
                st.announcer.play(str(data["name"]))
            elif self.path == "/api/announce/stop":
                st.announcer.stop()
            elif self.path == "/api/announce/delete":
                st.announcer.delete(str(data["name"]))
            elif self.path == "/api/announce/tts":
                return self._json({"ok": True, "name": st.announcer.tts(str(data["text"])[:2000],
                                                                        str(data.get("name", "station-id")))})
            elif self.path == "/api/compose":
                import composer
                return self._json({"ok": True, "text": composer.compose(str(data.get("kind")), st.info,
                                                                       data.get("fields") or {}, st.incidents)})
            elif self.path == "/api/ai_draft":
                import composer
                ctx = {"incident": st.incidents.active_or_latest(data.get("incident")) if data.get("use_incident") else None,
                       "fields": data.get("fields") or {}}
                if ctx["incident"]:
                    ctx["incident"] = {k: v for k, v in ctx["incident"].items() if k in ("kind", "mmsi", "vessel", "near", "last")}
                    ctx["incident"]["vessel"] = {k: v for k, v in (ctx["incident"].get("vessel") or {}).items()
                                                 if k not in ("nmea", "track")}
                return self._json({"ok": True, "text": composer.ai_draft(st.cfg.get("ai", {}), st.info, str(data.get("kind")),
                                                                         str(data.get("notes", ""))[:3000], ctx)})
            elif self.path == "/api/zone/add":
                st.watch.add_zone(data)
            elif self.path == "/api/zone/remove":
                st.watch.remove_zone(str(data.get("name")))
            elif self.path == "/api/watchlist/add":
                st.watch.add_watch(int(data["mmsi"]), str(data.get("label") or data["mmsi"]),
                                   str(data.get("note", "")), str(data.get("level", "info")))
            elif self.path == "/api/watchlist/remove":
                st.watch.remove_watch(int(data["mmsi"]))
            elif self.path == "/api/notify/test":
                if not st.notifier.channels():
                    raise ValueError("No forwarding channels configured in config.json (notify section)")
                st.notifier.send_alert({"key": f"test:{time.time()}", "level": "info", "kind": "Test",
                                        "title": "Test message from Ocean City Watch",
                                        "detail": "Alert forwarding is working.", "first": time.time(),
                                        "mmsi": None, "lat": st.lat, "lon": st.lon}, force=True)
            else:
                return self._send(404, b"{}")
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": True})


def serve(station, port: int, lan: bool = False) -> ThreadingHTTPServer:
    Handler.station = station
    Handler.allow_lan = lan
    httpd = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd
