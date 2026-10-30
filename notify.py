"""Forward alerts by email, Telegram, WhatsApp (Meta Cloud API) and webhook.

All sending happens on a background thread, so a slow mail server never
delays the alarm on screen. Each alert is sent at most once per 10 minutes.
"""

from __future__ import annotations

import json
import mimetypes
import queue
import smtplib
import ssl
import threading
import time
import urllib.request
from email.message import EmailMessage
from pathlib import Path

LEVELS = {"info": 0, "warn": 1, "crit": 2}


class Notifier:
    def __init__(self, cfg: dict, station_name: str) -> None:
        self.cfg = cfg or {}
        self.station = station_name
        self.min_level = LEVELS.get(self.cfg.get("min_level", "crit"), 2)
        self.q: queue.Queue = queue.Queue()
        self.sent: dict[str, float] = {}
        self.status: dict[str, str] = {}
        threading.Thread(target=self._run, daemon=True, name="notifier").start()

    def channels(self) -> list[str]:
        c = self.cfg
        out = []
        if c.get("email", {}).get("smtp_host") and c["email"].get("to"):
            out.append("email")
        if c.get("telegram", {}).get("bot_token") and c["telegram"].get("chat_ids"):
            out.append("telegram")
        if c.get("whatsapp", {}).get("access_token") and c["whatsapp"].get("to"):
            out.append("whatsapp")
        if c.get("webhook", {}).get("url"):
            out.append("webhook")
        return out

    def send_alert(self, a: dict, attachment: Path | None = None, force: bool = False) -> None:
        if not force and LEVELS.get(a["level"], 0) < self.min_level:
            return
        now = time.time()
        if not force and now - self.sent.get(a["key"], 0) < 600:
            return
        self.sent[a["key"]] = now
        self.q.put((a, attachment))

    # ------------------------------------------------------------------
    def _text(self, a: dict) -> tuple[str, str]:
        lvl = {"crit": "EMERGENCY", "warn": "WARNING", "info": "INFO"}[a["level"]]
        subject = f"[{self.station}] {lvl}: {a['title']}"
        lines = [subject, "", a.get("detail") or ""]
        if a.get("mmsi"):
            lines.append(f"MMSI: {a['mmsi']}")
        if a.get("lat") is not None:
            lat, lon = a["lat"], a["lon"]
            lines += [f"Position: {lat:.5f}, {lon:.5f}",
                      f"Map: https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=12/{lat:.5f}/{lon:.5f}"]
        lines.append(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(a['first']))}")
        lines.append(f"Sent automatically by Coastal Radio at {self.station}.")
        return subject, "\n".join(lines)

    def _run(self) -> None:
        while True:
            a, attach = self.q.get()
            subject, body = self._text(a)
            for name in self.channels():
                try:
                    getattr(self, f"_{name}")(subject, body, attach)
                    self.status[name] = f"sent {time.strftime('%H:%M:%S UTC', time.gmtime())}"
                except Exception as e:  # noqa: BLE001
                    self.status[name] = f"FAILED: {e}"
                    print(f"[notify] {name} failed: {e}")

    def _post_json(self, url: str, payload: dict, headers: dict | None = None) -> None:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()

    def _email(self, subject: str, body: str, attach: Path | None) -> None:
        c = self.cfg["email"]
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, c.get("from") or c.get("username"), ", ".join(c["to"])
        msg.set_content(body)
        if attach and attach.is_file():
            ctype = mimetypes.guess_type(attach.name)[0] or "application/octet-stream"
            main, sub = ctype.split("/", 1)
            msg.add_attachment(attach.read_bytes(), maintype=main, subtype=sub, filename=attach.name)
        port = int(c.get("smtp_port", 587))
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(c["smtp_host"], port, context=ctx, timeout=30) as s:
                if c.get("username"):
                    s.login(c["username"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(c["smtp_host"], port, timeout=30) as s:
                s.starttls(context=ctx)
                if c.get("username"):
                    s.login(c["username"], c["password"])
                s.send_message(msg)

    def _telegram(self, subject: str, body: str, attach: Path | None) -> None:
        c = self.cfg["telegram"]
        for chat in c["chat_ids"]:
            self._post_json(f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
                            {"chat_id": chat, "text": body[:4000], "disable_web_page_preview": False})

    def _whatsapp(self, subject: str, body: str, attach: Path | None) -> None:
        # Meta WhatsApp Cloud API. Messages a business starts must use an approved
        # template; plain text only works within 24 h of the recipient messaging you.
        c = self.cfg["whatsapp"]
        url = f"https://graph.facebook.com/{c.get('api_version', 'v21.0')}/{c['phone_number_id']}/messages"
        hdr = {"Authorization": f"Bearer {c['access_token']}"}
        for to in c["to"]:
            if c.get("template"):
                payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                           "template": {"name": c["template"], "language": {"code": c.get("language", "en")},
                                        "components": [{"type": "body", "parameters": [
                                            {"type": "text", "text": body.replace("\n", " · ")[:1000]}]}]}}
            else:
                payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": body[:4000], "preview_url": True}}
            self._post_json(url, payload, hdr)

    def _webhook(self, subject: str, body: str, attach: Path | None) -> None:
        self._post_json(self.cfg["webhook"]["url"], {"station": self.station, "subject": subject, "text": body})
