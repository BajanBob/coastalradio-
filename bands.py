"""Frequency lists for the HF console: marine HF, aviation HF families,
VOLMET and HFDL ground stations.

All frequencies are in kHz as published. "dial_offset" is what to add to the
published frequency to get the radio's USB dial frequency:
  - voice (marine and aviation): published = USB dial (carrier) frequency
  - HF DSC: published = centre of the 1615/1785 Hz tones -> dial 1.7 kHz lower
  - HFDL: tune 1.44 kHz lower so the signal sits mid-passband

Sources: ITU Radio Regulations Appendices 15 and 17 (marine), ICAO MWARA
allotments as listed by RadioReference (aviation families), and the dumphfdl
HFDL system table (version 52) for HFDL ground stations. Frequencies change:
edit or add your own in config.json -> "hf" -> "extra_channels".
"""

from __future__ import annotations

MARINE = {
    "Distress & safety voice (USB)": [2182, 4125, 6215, 8291, 12290, 16420],
    "HF DSC distress & safety": [2187.5, 4207.5, 6312, 8414.5, 12577, 16804.5],
    "Ship-to-ship simplex (App. 17)": {"4A": 4146, "4B": 4149, "6A": 6224, "6B": 6227, "6C": 6230,
                                       "8A": 8294, "8B": 8297, "12A": 12353, "12B": 12356, "16A": 16528},
    "USCG offshore weather voice (NMN Chesapeake)": [4426, 6501, 8764, 13089, 17314],
    "USCG offshore weather voice (NMG New Orleans)": [4316, 8502, 12788],
}

# ICAO Major World Air Route Area families (USB voice) and the stations that use them.
AVIATION = {
    "CAR-A": ("Caribbean: New York Radio", [2887, 3455, 5550, 6577, 8846, 11396]),
    "CAR-B": ("Caribbean: New York Radio", [5520, 6586, 8918, 11330, 13297, 17907, 21985]),
    "NAT-A": ("N Atlantic: Gander, Shanwick (Shannon), New York, Santa Maria",
              [3016, 5598, 8906, 11396, 13306, 17946, 21925]),
    "NAT-B": ("N Atlantic: Gander, Shanwick (Shannon), Iceland", [2899, 5616, 8864, 13291]),
    "NAT-C": ("N Atlantic: Gander, Shanwick (Shannon), Iceland", [2872, 5649, 8879, 11336]),
    "NAT-D": ("N Atlantic: Gander, Shanwick (Shannon), Iceland, Bodø", [2971, 4675, 8891, 13291]),
    "NAT-E": ("N Atlantic: New York, Santa Maria", [2962, 6628, 8825, 11309, 13354, 17952, 21964]),
    "NAT-F": ("N Atlantic: Gander, Shanwick (Shannon)", [3476, 6622, 8831, 13291]),
    "SAT-1": ("S Atlantic: Dakar, Atlântico/Recife, Sal, Canarias, Johannesburg",
              [3452, 6535, 8861, 13357, 17955]),
    "SAT-2": ("S Atlantic: Dakar, Atlântico/Recife, Sal, Canarias", [2854, 5565, 11291, 13315, 17955]),
    "CEP-1": ("Central East Pacific: San Francisco", [3413, 5574, 8843, 8915, 13354]),
    "CEP-2": ("Central East Pacific: San Francisco", [2869, 3452, 5547, 6673, 10057, 11282, 13288, 21964]),
    "NP": ("North Pacific: San Francisco, Anchorage, Tokyo", [5628, 6655, 8951, 10048, 13339, 17946, 21925]),
    "LDOC SFO": ("San Francisco long-distance (company)", [3007, 6637, 10072, 13339, 17940]),
}

VOLMET = {
    "New York VOLMET": [3485, 6604, 10051, 13270],
    "Gander VOLMET": [3485, 6604, 10051, 13270],
    "Shannon VOLMET": [3413, 5505, 8957, 13264],
}

# HFDL ground stations (dumphfdl system table v52).
HFDL = {
    "San Francisco": [21934, 17919, 13276, 11327, 10081, 8927, 6559, 5508],
    "Molokai, Hawaii": [21937, 17919, 13324, 13312, 13276, 11348, 11312, 10027, 8936, 8912, 6565, 5514],
    "Reykjavik": [17985, 15025, 11184, 8977, 6712, 5720, 3900],
    "Riverhead, New York": [21931, 17919, 13276, 11387, 8912, 6661, 5652],
    "Auckland": [17916, 13351, 10084, 8921, 6535, 5583],
    "Hat Yai, Thailand": [21949, 17928, 13270, 10066, 8825, 6535, 5655],
    "Shannon": [11384, 10081, 8942, 8843, 6532, 5547, 3455, 2998],
    "Johannesburg": [21949, 17922, 13321, 11321, 8834, 5529, 4681, 3016],
    "Barrow, Alaska": [21937, 21928, 17934, 17919, 11354, 10093, 10027, 8936, 8927, 6646, 5544, 5538, 5529,
                       4687, 4654, 3497, 3007, 2992, 2944],
    "Muan, South Korea": [21931, 17958, 13342, 10060, 8939, 6619, 5502, 2941],
    "Albrook, Panama": [17901, 13264, 10063, 8894, 6589, 5589],
    "Santa Cruz, Bolivia": [21997, 17916, 13315, 11318, 8957, 6628, 4660],
    "Krasnoyarsk": [21990, 17912, 13321, 10087, 8886, 6596, 5622],
    "Al Muharraq, Bahrain": [21982, 17967, 13312, 10030, 8885, 6646, 5544, 2986],
    "Agana, Guam": [21928, 17919, 13312, 11306, 8927, 6652, 5451],
    "Canarias": [21955, 17928, 13303, 11348, 8948, 6529],
}

DSC_DISTRESS = [2187.5, 4207.5, 6312, 8414.5, 12577, 16804.5]
VOICE_DISTRESS = [2182, 4125, 6215, 8291, 12290, 16420]


def _ch(group: str, label: str, khz: float, offset: float = 0.0, kind: str = "voice", mode: str = "USB") -> dict:
    return {"id": f"{group}|{label}|{khz:g}", "group": group, "label": label, "khz": khz,
            "dial_khz": round(khz + offset, 2), "mode": mode, "kind": kind}


def channels(extra: list[dict] | None = None) -> list[dict]:
    """Flat list of every selectable channel, grouped for the dashboard."""
    out = []
    for group, freqs in MARINE.items():
        dsc = "DSC" in group
        items = freqs.items() if isinstance(freqs, dict) else ((f"{f:g} kHz", f) for f in freqs)
        for label, f in items:
            out.append(_ch("Marine: " + group, label if isinstance(freqs, dict) else f"{f:g}", f,
                           -1.7 if dsc else 0.0, "dsc" if dsc else "voice"))
    for fam, (who, freqs) in AVIATION.items():
        for f in freqs:
            out.append(_ch(f"Aviation {fam}: {who}", f"{f:g}", f, kind="aviation"))
    for name, freqs in VOLMET.items():
        for f in freqs:
            out.append(_ch(f"VOLMET: {name}", f"{f:g}", f, kind="volmet"))
    for name, freqs in HFDL.items():
        for f in sorted(freqs):
            out.append(_ch(f"HFDL: {name}", f"{f:g}", f, -1.44, "hfdl"))
    for e in extra or []:
        try:
            out.append(_ch(e.get("group", "My channels"), str(e.get("label", e["khz"])), float(e["khz"]),
                           float(e.get("dial_offset_khz", 0)), e.get("kind", "voice"), e.get("mode", "USB").upper()))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def scan_lists() -> dict[str, list[float]]:
    """Ready-made scan lists (published kHz)."""
    return {
        "Marine distress voice": VOICE_DISTRESS,
        "HF DSC distress": DSC_DISTRESS,
        "Caribbean air (CAR-A + CAR-B)": AVIATION["CAR-A"][1] + AVIATION["CAR-B"][1],
        "North Atlantic air (NAT-A..F)": sorted({f for k, (_, fs) in AVIATION.items() if k.startswith("NAT") for f in fs}),
        "South Atlantic air (SAT-1/2)": sorted(set(AVIATION["SAT-1"][1] + AVIATION["SAT-2"][1])),
        "VOLMET": sorted({f for fs in VOLMET.values() for f in fs}),
    }
