# Ocean City Watch

AIS and search-and-rescue watch-keeping software for **Ocean City Radio, 8PFX**. It runs on the station computer alongside your marine and amateur radios:

- **Receives and decodes AIS** with your IC-9100, FT-991A and FTM-510DR, or any receiver with NMEA output, and shows every vessel on a live chart.
- **Raises emergency alarms** for AIS-SART, MOB and EPIRB-AIS beacons, distress status and distress text, with a flashing red screen and a siren.
- **Opens an incident file** for each emergency, with a printable report, track, nearby vessels that could assist, and a draft MAYDAY RELAY for the operator.
- **Forwards alerts** by email, Telegram, WhatsApp or webhook.
- **Records voice traffic** for SAR (VHF Ch16, IC-M710, Barrett 2050) as timestamped clips whenever someone speaks.
- **Keeps logs:** daily AIS logs and on-demand snapshots of everything on screen.
- **Prepares station announcements:** call sign (phonetic), position, grid square, recorded or text-to-speech.
- **HF SSB receiver console:** CAT-tunes your HF rig in USB. The computer adds noise reduction, auto-notch, AGC, a waterfall, PC-speaker audio, scanning that stops on voice, and a recording of every transmission. It has marine, aviation (CAR, NAT, SAT, CEP, NP), VOLMET and HFDL channel lists, plus free dial-frequency entry.
- **SELCAL and HF DSC decoders** run on the HF audio. SELCAL codes can be put on a watch list. HF DSC distress alerts open incidents just like VHF DSC.
- **RS41 radiosonde decoder** (needs an SDR on 400–406 MHz): balloon track, burst detection and a rough landing estimate on the chart.
- **HFDL / HF ACARS / CPDLC, Inmarsat Aero ACARS and Inmarsat-C SafetyNET** through the free decoders dumphfdl, JAERO and Scytale-C (each needs an SDR). Ocean City Watch plots the aircraft, keeps the message logs, flags distress and urgency traffic, and can ask Claude to explain a message.
- **One station timeline** of everything heard and done.

**Ocean City Watch is receive-only.** It tunes radios (frequency and receive mode) and plays announcement audio only when the operator presses Play. It has no PTT or transmit function. AIS responses must come from your type-approved AIS and DSC equipment, and voice transmissions are made by the operator.

## Quick start
Requires Python 3.9+.
```bash
pip install -r requirements.txt
python ocw.py --demo          # simulated ships; an AIS-SART activates after ~25 s
python ocw.py --list-audio    # find your radios' sound inputs and outputs
python ocw.py                 # first run creates config.json; edit it, then run again
```
The dashboard opens at `http://127.0.0.1:8100`. Click **Arm alarm sound** once; browsers only allow sound after a click.

## Console view and faceplates
**▣ Console** puts the whole station on one screen for a large monitor (it opens automatically on screens 1600 px wide or more):
- the chart with vessels, aircraft and radiosondes
- the HF receiver: frequency readout, tuning, scan, processing and recent activity
- the waterfall and the alert list with Ack buttons
- station status, the DSC and SELCAL calls, and the timeline

It rearranges itself into one column on smaller screens. Press F11 in the browser for full screen.

Pick a **faceplate** at the top right; your choice is remembered on that computer:
- **Ocean City:** deep blue, cyan and green readouts, red alarms.
- **Admiralty Gold:** navy and brass bezels, amber frequency digits, green highlights, gold waterfall.
- **Rescue Red:** charcoal with red and gold trim, blue frequency digits.
- **Night:** red on black only, with a brightness slider. The chart, waterfall, icons and alarms all turn dim red to protect night vision.

Tick **auto night** and the Night faceplate switches on by itself after sunset at the station's position, and back at sunrise.

## Dashboard
| Tab | What's there |
|---|---|
| **Watch** | Live chart (OpenStreetMap; range-and-bearing plot when offline), AIS receiver status and channel A/B/Scan, vessel list and details, live NMEA |
| **Emergencies** | All alerts with acknowledge buttons (who and when is logged), and incident files with their reports |
| **SAR recorder** | Live level meters for each recorder, "Record 10 min" button, recent recordings with playback |
| **Announcements** | Station ID (lat/lon, grid square), announcement script, record-your-voice, text-to-speech, playback to radio |
| **HF radio** | Frequency readout, dial entry, channel lists, scan, noise reduction / notch / AGC, waterfall, SELCAL log, transmissions heard |
| **Aviation & data** | Aircraft from HFDL/ACARS, HFDL/CPDLC messages, Inmarsat-C safety messages with AI explanation, ACARS, radiosondes, feed status |
| **Timeline** | Every alert, call, SELCAL, recording, data message and radiosonde event in one list (filterable), with playback |
| **Station & forwarding** | Station details, forwarding status with a **Send test** button, message statistics |

Use **📸 Snapshot** at any time to save the current picture (vessels table, CSV, full data) to `data/snapshots/`.

## Emergencies
| Detected | How | Level |
|---|---|---|
| AIS-SART, MOB device, EPIRB-AIS | MMSI 970…, 972…, 974… transmitting position or "ACTIVE" | **EMERGENCY** |
| Distress status | Any vessel with navigational status 14 | **EMERGENCY** |
| Distress text | Safety message containing MAYDAY, SOS, DISTRESS, SINKING, FIRE… | **EMERGENCY** |
| Safety broadcasts | Other AIS safety messages (types 12 and 14) | Warning |
| Beacon tests | "SART TEST", "MOB TEST" | Info |
| SAR aircraft | AIS type 9 | Info (shown on the chart) |

When an emergency is detected:
1. The screen flashes red, the siren sounds every few seconds until acknowledged, and a desktop notification appears.
2. The beacon is pinned to the top of the vessel list and drawn as a pulsing red marker.
3. An **incident folder** opens in `data/incidents/` and is kept up to date. It contains `report.html` (position in marine format and decimal, grid square, range and bearing from 8PFX, track plot, **vessels within 15 nm that could assist**, a draft **MAYDAY RELAY**, timeline), `nmea.txt` (raw AIS) and copies of every voice recording made during the incident.
4. The alert is **forwarded** to your configured channels with an OpenStreetMap link and the report attached to the email.

Treat the MAYDAY RELAY as a draft. Coordinate with your MRCC and follow national procedures before transmitting.

## DSC watch on Channel 70 (`dsc_receivers`)
Connect any receiver tuned to **VHF Ch70 (156.525 MHz, FM)** to a sound input. A CAT-controlled rig is tuned automatically; for a marine receiver, use `"rig": "manual"`. Ocean City Watch decodes DSC calls (ITU-R M.493: 1200-baud FSK, time diversity, error-check character):
- **Distress alerts and distress relays:** EMERGENCY. The casualty goes on the chart, an incident opens with nature of distress, position and time, and the alert is forwarded.
- **Urgency calls:** warning.
- **Safety, routine and test calls, and acknowledgements:** logged on the DSC tab and in `data/logs/dsc-YYYYMMDD.log`.

In tests with synthesized calls, decoding stayed above 95% at very low signal levels, with no false calls from noise. Check it with a real routine or test call from one of your radios before relying on it.

## Traffic watch (`traffic_watch`)
| Feature | What it does | Settings |
|---|---|---|
| **Geofences** | Alerts when a vessel enters a reef, restricted, harbour, anchorage or marine-park zone. Zones are drawn on the chart. Add circles from the Traffic watch tab (click the chart to pick a position) or polygons in `data/zones.json` | per-zone `level` |
| **Collision risk** | Warns when two moving vessels will pass closer than the limit within the time window, and draws a dashed line between them | `cpa_nm` 0.5, `tcpa_min` 15, `cpa_range_nm` 25 |
| **AIS gaps** | Warns when a moving vessel well inside your coverage stops transmitting, and reports when it reappears and how far it moved | `dark_range_nm` 15, `dark_minutes_a` 10, `dark_minutes_b` 15 |
| **Anchor watch** | Tracks vessels reporting "at anchor" (or stopped inside an anchorage zone) and warns if one moves more than the limit from its anchor position | `anchor_radius_m` 150 |
| **Watchlist** | Alerts when listed vessels come into range, at the level you choose, with your note | managed on the Traffic watch tab |
| **AIS channels** | Shows whether both AIS 1 and AIS 2 are being heard; ships alternate between them | — |

## Coverage map
Every position heard builds a picture of your station's reach: best range in each 10° sector (Coverage tab) and the chart squares where ships are heard (tick "Show … on the Watch chart"). Saved in `data/coverage.json`. Weak sectors show where to improve the antenna.

## Sharing with AIS networks (`share`)
Add the host and port that AISHub, MarineTraffic or another network gives you. Your decoded AIS is sent to them, and many networks give you free wider-area data in return. Sharing is turned off in demo mode.

## Broadcast composer and Press To Play
On the **Broadcast** tab:
1. **Compose.** Choose a template: station identification, Sécurité navigational warning, Sécurité weather, Pan-Pan urgency broadcast, or MAYDAY RELAY (filled from the current incident). Add a title, details and a position (click the chart to fill it), then press **Fill from template**.
2. **Or draft with AI (optional).** Write your notes and press **✨ Draft with AI**. The draft follows standard radiotelephone procedure and uses only the facts you gave; anything missing is marked `[MISSING: …]` for you to fill in. This needs an Anthropic API key under `ai` in config.json (`model`, `api_version` and `endpoint` are configurable). Your notes and any incident data you include are sent to the API.
3. **Review and edit** the script in the text box. You are responsible for every word.
4. **Make audio** with text-to-speech (`pip install pyttsx3`), or record your own voice.
5. **▶ PRESS TO PLAY.** Select the audio and press the button. A checklist appears: correct working channel, channel clear, script reviewed and you will key the transmitter. **Play now** only unlocks when all three are ticked. The audio then plays to `announcements.output_device`, the output wired to your marine radio's audio or mic input.

Ocean City Watch never keys a radio and never plays anything on its own. Every broadcast starts with the operator, on a channel the operator chose, under your coast station licence and MRCC procedures.

## Alert forwarding (`notify` in config.json)
- **min_level:** `crit` (emergencies only, the default), `warn` or `info`.
- **email:** any SMTP server. For Gmail, use smtp.gmail.com, port 587 and an app password.
- **telegram:** create a bot with @BotFather, then put its token and your chat or group ID here. This is the quickest to set up and free.
- **whatsapp:** uses Meta's official WhatsApp Business Cloud API (phone number ID, access token, recipient numbers). WhatsApp only lets businesses start conversations with an **approved message template**. Create one with a single `{{1}}` variable and put its name in `template`. Without a template, messages only reach people who messaged your business number in the last 24 hours.
- **webhook:** POSTs JSON to any URL, for example a Teams, Slack or your own server.

Each alert is forwarded at most once every 10 minutes. Use **Send test** on the Station tab to check your settings.

## SAR voice recorder (`recorders`)
Connect each radio's speaker or line output to a sound card or USB audio interface. Typical sources are the Ch16 watch receiver, the **IC-M710** on 2182 kHz and the **Barrett 2050** on HF. A clip is saved whenever the level rises above `threshold_db` (adjust it using the meter: the white mark is the threshold). Clips include `preroll_seconds` of audio before the voice started. Files are `data/recordings/YYYY-MM-DD/HHMMSSZ_<name>.wav` in UTC, kept for `retention_days`. Copies inside incident folders are kept permanently.

## Station announcements
The Announcements tab builds your identification script from config.json:
> THIS IS OCEAN CITY RADIO … CALL SIGN EIGHT PAPA FOXTROT X-RAY. COAST STATION POSITION 13 DEGREES 05.85 MINUTES NORTH, 059 DEGREES 37.00 MINUTES WEST. GRID SQUARE GOLF KILO ZERO THREE ECHO CHARLIE …

You can read the script yourself, record it with the computer's microphone, or make a text-to-speech version (`pip install pyttsx3`). Customise it with `announcement_template` using the placeholders `{name} {callsign} {callsign_phonetic} {position_spoken} {lat_dm} {lon_dm} {grid} {grid_phonetic} {lat} {lon} {extra}`.

**Playing to the radio:** set `announcements.output_device` to the audio output wired to your marine radio's audio or mic input. **Play to radio** asks you to confirm the channel and that it's clear, then plays the audio. You key the transmitter, or use the radio's VOX or data interface. `reminders` (e.g. `["08:00", "20:00"]`) raises a reminder alert when a scheduled broadcast is due; it never broadcasts by itself.

**Showing 8PFX on vessels' screens:** an announcement is heard, not displayed. For 8PFX to appear on ships' AIS displays, the station needs a type-approved **AIS base station**, which transmits its position automatically. That equipment is separate from this software.

## HF SSB receiver (`hf_receivers`)
Your radio demodulates SSB. Ocean City Watch tunes it by CAT, takes its USB audio and does the rest on the computer:
- **Noise reduction** tracks the noise at each audio frequency and removes it. **Strength** sets how hard: higher is quieter, but voices start to sound "watery".
- **Auto-notch** finds steady carriers and heterodyne whistles (anything constant for several seconds) and cuts them. Speech and SELCAL tones are left alone.
- **AGC** brings weak and strong stations to the same level. The **waterfall** shows the 0–3 kHz receive passband.
- **Scan** steps through a list (marine distress voice, HF DSC, Caribbean air, North Atlantic air, VOLMET, or your own lists). It stops when a signal comes up, records it and moves on a few seconds after it ends. Recordings go to the SAR recorder tab and the timeline.
- **Dial frequency** takes any kHz. For voice, type the published frequency. Use offset −1.7 kHz for HF DSC and −1.44 kHz to listen to HFDL; the channel lists apply these offsets for you.
- **PC speakers:** set `output_device` to hear the processed audio. Leave it `null` to listen on the radio.

**Radios.** Supported: Icom CI-V (IC-9100, IC-7300, IC-7610, IC-705, IC-R8600…), Yaesu (FT-991A) and Kenwood. Shortcuts such as `"rig": "ic9100"` set the CI-V address for you. `"model"` lets the console refuse frequencies the radio can't receive. Use the radio's USB audio (the IC-9100 and FT-991A have built-in USB sound cards) and set the radio's SSB filter to about 2.8–3.0 kHz. A radio can only do one job at a time: a rig on AIS can't also be your HF receiver. The IC-M710 and Barrett 2050 have no CAT interface here, but they can still be recorded with `recorders`.

**IC-9700.** Use `"rig": "ic9700"` (CI-V address A2h). It receives only 144–148, 430–450 and 1240–1300 MHz, so it can't receive marine VHF, AIS, HF or radiosondes (400–406 MHz). In Ocean City Watch it works as a 2 m / 70 cm receiver with the same processing, scanning and recording.

**SELCAL** decodes on any channel. Put codes you care about in `selcal_watch` (for example a local operator's aircraft) to get an alert. **HF DSC** decodes whenever you're tuned to a DSC frequency (or scanning the HF DSC list). Distress alerts open an incident and appear on the chart.

## Radiosondes (`radiosondes`)
Barbados launches RS41-type weather balloons from Grantley Adams. They transmit on 400–406 MHz, which the IC-9100, FT-991A and IC-9700 can't receive, so you need an **SDR** (an RTL-SDR v4 is about US$40) and a 70 cm or 400 MHz antenna.
1. In SDR++ or SDR#, tune the sonde (often 403.0 MHz; watch the waterfall around launch times). Set **NFM**, bandwidth about **15 kHz**, and turn **de-emphasis off**.
2. Send the audio to a virtual audio cable (VB-CABLE) and set that device as `audio_device`.

Ocean City Watch shows serial, altitude, climb rate, speed, track, burst and a rough landing point. Only Vaisala RS41 is decoded (not M10/M20 or others).

## HFDL, HF ACARS, CPDLC and Inmarsat (`feeds`)
These need an SDR and a dedicated decoder program. Ocean City Watch receives what they decode over UDP. The decoders can run on this PC, a Raspberry Pi or WSL.

| Feed | Decoder | Hardware | Send to |
|---|---|---|---|
| HFDL, HF ACARS, CPDLC, ADS-C | **dumphfdl** (Linux / macOS / Raspberry Pi / WSL) | HF SDR: Airspy HF+, SDRplay RSP, RTL-SDR v4 (direct sampling) | `--output decoded:json:udp:address=<this PC>,port=5555` |
| Inmarsat Aero ACARS (aircraft satellite) | **JAERO** (Windows) | SDR, L-band patch antenna and LNA (1.5 GHz) | its UDP/JSON output → port 5556 |
| Inmarsat-C SafetyNET, NAVAREA IV, SAR broadcasts | **Scytale-C** (Windows) | same as JAERO | its UDP output → port 5557 |

Example dumphfdl command for the 8 MHz HFDL band on an Airspy HF+:
`dumphfdl --soapysdr driver=airspyhf --sample-rate 912000 --centerfreq 8900 8912 8927 8942 8948 8957 8977 --output decoded:json:udp:address=192.168.3.169,port=5555`

The parser looks for registration, flight, position, message text and CPDLC anywhere in each message, so different program versions work. Aircraft with a position appear on the chart in yellow. Messages containing distress or urgency wording (MAYDAY, PAN PAN, EPIRB, SAR, 7700…) raise an alert. With an Anthropic key under `ai`, Claude adds a plain-English explanation and distance from the station to those messages; any message can be explained with **Explain**. When Windows asks, allow Ocean City Watch on private networks.

These are listening tools. Messages decoded from aviation and satellite links are for your own awareness; check the rules in Barbados about sharing their content.

## AIS receivers (`receivers`)
Supported CAT control: Icom CI-V (IC-9100, address `0x7C`), Yaesu (FT-991A) and Kenwood ASCII CAT. Use `manual` for the FTM-510DR. Use the radios' **9600 data output** for audio; the speaker jack usually filters too much for 9600-baud AIS. Set the input level so the meter reads about -20 to -10 dBFS on a busy channel.

The HF radios (IC-M710, Barrett 2050) and the IC-M330 don't receive AIS. A marine AIS receiver or transponder can feed in through `nmea_in_serial` (usually 38400 baud). An RTL-SDR running AIS-catcher can feed in through `nmea_in_udp`.

## Files
```
data/logs/ais-YYYYMMDD.nmea        every AIS sentence, one file per UTC day (replayable in OpenCPN)
data/logs/alerts.log               every alert and acknowledgement
data/incidents/<id>/               report.html, incident.json, nmea.txt, recordings/
data/snapshots/<time>/             snapshot.html, vessels.csv, state.json
data/recordings/YYYY-MM-DD/        voice clips
data/announcements/                announcement library
data/logs/timeline-YYYYMMDD.log    station timeline
data/logs/sondes-YYYYMMDD.log      radiosonde positions
data/logs/hfdl|acars|inmarsat-YYYYMMDD.log   raw decoded data messages
```

## Tests
`python tests/test_pipeline.py && python tests/test_station.py && python tests/test_features.py && python tests/test_hf.py`. The HF suite covers SELCAL in noise, HF DSC, noise reduction, auto-notch, voice detection, RS41 frames (both polarities), feed parsing, CAT modes and the full station wiring. The last suite covers DSC decoding (distress, relay, urgency, test calls), collision geometry, geofences, watchlist, anchor drag, AIS gaps, coverage, the composer templates and AI drafting against a stand-in API. The other suites check the AIS decoder against synthesized signals, emergency classification, the grid square and position formats, the complete emergency flow (alarm, incident report, forwarding, recording capture) and snapshots.

## License
MIT
