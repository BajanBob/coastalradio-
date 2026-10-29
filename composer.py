"""Broadcast composer: builds voice-broadcast scripts from templates and live data,
optionally drafted by AI (Anthropic API) from the operator's notes.

Every script is shown to the operator to review and edit. Nothing is ever
played or transmitted automatically: audio only goes to the radio when the
operator presses "Press To Play" and keys the transmitter.
"""

from __future__ import annotations

import json
import re
import urllib.request

from announce import dm, phonetic, script, spoken_position


def _hdr(st: dict) -> tuple[str, str]:
    name = st.get("name", "COAST STATION").upper()
    call = st.get("callsign", "")
    return name, f"{name} {phonetic(call)}".strip()


def securite(st: dict, title: str, body: str, lat=None, lon=None, channel: str = "") -> str:
    name, who = _hdr(st)
    pos = f" IN POSITION {spoken_position(lat, lon)}." if lat is not None else ""
    ch = f" THIS MESSAGE IS ON CHANNEL {channel}." if channel else ""
    return (f"SECURITE, SECURITE, SECURITE. ALL STATIONS, ALL STATIONS, ALL STATIONS. "
            f"THIS IS {who}, {who}, {who}.{ch} {title.upper()}.{pos} {body.upper()} {name} OUT.")


def pan_pan(st: dict, body: str, lat=None, lon=None) -> str:
    name, who = _hdr(st)
    pos = f" POSITION {spoken_position(lat, lon)}." if lat is not None else ""
    return (f"PAN PAN, PAN PAN, PAN PAN. ALL STATIONS, ALL STATIONS, ALL STATIONS. "
            f"THIS IS {who}, {who}, {who}. {body.upper()}.{pos} ALL VESSELS IN THE AREA KEEP A SHARP LOOKOUT "
            f"AND REPORT TO {name}. THIS IS {who}. OVER.")


def weather(st: dict, area: str, body: str) -> str:
    name, who = _hdr(st)
    return (f"SECURITE, SECURITE, SECURITE. ALL STATIONS, ALL STATIONS, ALL STATIONS. THIS IS {who}. "
            f"WEATHER INFORMATION FOR {area.upper() or 'COASTAL WATERS'}. {body.upper()} {name} OUT.")


TEMPLATES = {
    "station_id": "Station identification",
    "securite": "Sécurité: navigational warning",
    "weather": "Sécurité: weather information",
    "pan_pan": "Pan-Pan: urgency broadcast",
    "mayday_relay": "MAYDAY RELAY (from an incident)",
}


def compose(kind: str, st: dict, fields: dict, incidents=None) -> str:
    f = {k: (v or "") for k, v in fields.items()}
    lat = float(f["lat"]) if f.get("lat") not in ("", None) else None
    lon = float(f["lon"]) if f.get("lon") not in ("", None) else None
    if kind == "station_id":
        return script(st, f.get("text", "")).get("text", "")
    if kind == "securite":
        return securite(st, f.get("title") or "NAVIGATIONAL WARNING", f.get("text", ""), lat, lon, f.get("channel", ""))
    if kind == "weather":
        return weather(st, f.get("title", ""), f.get("text", ""))
    if kind == "pan_pan":
        return pan_pan(st, f.get("text", ""), lat, lon)
    if kind == "mayday_relay":
        if not incidents:
            raise ValueError("No incident data")
        inc = incidents.active_or_latest(f.get("incident"))
        if not inc:
            raise ValueError("No incident to relay. MAYDAY RELAY drafts come from emergency incidents.")
        return incidents.mayday_relay(inc).replace("\n", " ")
    raise ValueError(f"Unknown template {kind}")


AI_SYSTEM = """You draft marine VHF radiotelephone broadcasts for a coast station, to be read aloud or turned into speech.
Rules:
- Follow standard maritime radiotelephone procedure (ITU Radio Regulations, IMO SMCP): correct prowords
  (MAYDAY RELAY, PAN PAN, SECURITE, ALL STATIONS, THIS IS, OVER, OUT), each repeated three times where procedure requires.
- Station: {name}, call sign {callsign} (say it phonetically: {callsign_phonetic}). Station position {lat_dm} {lon_dm}.
- Positions in degrees and minutes, North/South, East/West. Times in UTC with the word UTC.
- Use ONLY facts in the operator's notes and the data provided. Never invent vessels, positions, times or hazards.
  If something essential is missing, write [MISSING: what] so the operator can fill it in.
- Plain capital letters, short sentences, no markdown, no commentary. Output only the script."""


def ai_draft(ai_cfg: dict, st: dict, kind: str, notes: str, context: dict) -> str:
    key = (ai_cfg or {}).get("api_key")
    if not key:
        raise RuntimeError("AI drafting is off. Add your Anthropic API key under \"ai\" in config.json.")
    la, lo = dm(st["lat"], st["lon"]) if st.get("lat") is not None else ("?", "?")
    system = AI_SYSTEM.format(name=st.get("name", ""), callsign=st.get("callsign", ""),
                              callsign_phonetic=phonetic(st.get("callsign", "")), lat_dm=la, lon_dm=lo)
    user = (f"Type of broadcast: {TEMPLATES.get(kind, kind)}\n\nOperator's notes:\n{notes.strip()}\n\n"
            f"Live data (JSON):\n{json.dumps(context, default=str)[:6000]}")
    return ai_call(ai_cfg, system, user, 800)


def ai_call(ai_cfg: dict, system: str, user: str, max_tokens: int = 800) -> str:
    """One request to the Anthropic Messages API using the "ai" settings in config.json."""
    key = (ai_cfg or {}).get("api_key")
    if not key:
        raise RuntimeError("AI is off. Add your Anthropic API key under \"ai\" in config.json.")
    body = {"model": ai_cfg.get("model", "claude-sonnet-5-5"), "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    headers = {"Authorization": f"Bearer {key}", "x-api-key": key,
               "anthropic-version": ai_cfg.get("api_version", "2023-06-01"), "content-type": "application/json"}
    if ai_cfg.get("workspace_id"):  # needed when the key isn't scoped to a workspace
        headers["anthropic-workspace-id"] = ai_cfg["workspace_id"]
    req = urllib.request.Request(ai_cfg.get("endpoint", "https://api.anthropic.com/v1/messages"),
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"AI request failed ({e.code}): {e.read()[:300].decode(errors='replace')}") from e
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return re.sub(r"\s+\n", "\n", text).strip()
