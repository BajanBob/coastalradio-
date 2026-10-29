"""Alert book: de-duplicates alerts, tracks acknowledgement, logs to disk."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

LEVELS = {"info": 0, "warn": 1, "crit": 2}


@dataclass
class Alert:
    key: str
    level: str
    kind: str
    title: str
    detail: str = ""
    mmsi: int | None = None
    lat: float | None = None
    lon: float | None = None
    incident: str | None = None
    first: float = field(default_factory=time.time)
    last: float = field(default_factory=time.time)
    count: int = 1
    acked: bool = False
    acked_by: str | None = None


class AlertBook:
    def __init__(self, log_dir: Path, on_new: Callable[[Alert], None] | None = None) -> None:
        self.alerts: dict[str, Alert] = {}
        self.lock = threading.Lock()
        self.on_new = on_new
        self.log = log_dir / "alerts.log"
        log_dir.mkdir(parents=True, exist_ok=True)

    def raise_(self, key: str, level: str, kind: str, title: str, detail: str = "", **extra) -> Alert:
        now = time.time()
        fresh = None
        with self.lock:
            a = self.alerts.get(key)
            if a and (not a.acked or now - a.last < 900):
                a.last, a.count = now, a.count + 1
                a.detail = detail or a.detail
                for k, v in extra.items():
                    if v is not None:
                        setattr(a, k, v)
                if LEVELS[level] > LEVELS[a.level]:
                    a.level, a.acked = level, False
                    fresh = a
            else:
                a = Alert(key, level, kind, title, detail, **extra)
                self.alerts[key] = a
                fresh = a
                if len(self.alerts) > 500:
                    oldest = min(self.alerts.values(), key=lambda x: x.last)
                    self.alerts.pop(oldest.key, None)
        if fresh:
            self._write(fresh)
            if self.on_new:
                self.on_new(fresh)
        return a

    def _write(self, a: Alert) -> None:
        try:
            with self.log.open("a", encoding="utf-8") as f:
                pos = f" {a.lat:.5f},{a.lon:.5f}" if a.lat is not None else ""
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(a.first))}  {a.level.upper():4}  "
                        f"{a.kind}  {a.title}{pos}  {a.detail}\n")
        except OSError:
            pass

    def ack(self, key: str | None, who: str = "operator") -> None:
        with self.lock:
            for a in self.alerts.values():
                if (key is None or a.key == key) and not a.acked:
                    a.acked, a.acked_by = True, f"{who} at {time.strftime('%H:%M:%S UTC', time.gmtime())}"
                    try:
                        with self.log.open("a", encoding="utf-8") as f:
                            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  ACK   {a.title} ({a.acked_by})\n")
                    except OSError:
                        pass

    def snapshot(self) -> list[dict]:
        with self.lock:
            items = sorted(self.alerts.values(), key=lambda a: (a.acked, -LEVELS[a.level], -a.last))
            return [asdict(a) for a in items]

    def alarm(self) -> str | None:
        with self.lock:
            levels = {a.level for a in self.alerts.values() if not a.acked}
        return "crit" if "crit" in levels else "warn" if "warn" in levels else None
