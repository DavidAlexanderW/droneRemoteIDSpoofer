#!/usr/bin/env python3
"""
ANSI/CTA-2063-A Drone Remote ID Serial Number Decoder & Model Inference Engine.

Decodes 4-character ICAO/CTA Manufacturer Codes (MFR) and hardware generation
sub-type prefixes into human-readable drone make, model family, and manufacturer country.
"""

from typing import Any, Dict, Optional

# CTA-2063-A 4-character Manufacturer Prefix Database
# Standard format: MFR code (base32/alphanumeric, chars 1-4)
CTA_MANUFACTURERS: Dict[str, Dict[str, str]] = {
    # Major Consumer & Enterprise OEMs
    "1581": {"make": "DJI", "company": "SZ DJI Technology Co., Ltd.", "country": "China"},
    "1596": {"make": "Autel Robotics", "company": "Autel Robotics Co., Ltd.", "country": "China / USA"},
    "1668": {"make": "Skydio", "company": "Skydio, Inc.", "country": "United States"},
    "1748": {"make": "Parrot", "company": "Parrot Drones SAS", "country": "France"},
    "1787": {"make": "Yuneec", "company": "Yuneec International", "country": "China"},
    "1805": {"make": "Holy Stone", "company": "Holy Stone Enterprise Co., Ltd.", "country": "China"},
    "1549": {"make": "Teledyne FLIR", "company": "Teledyne FLIR LLC", "country": "United States"},
    "1686": {"make": "Wingtra", "company": "Wingtra AG", "country": "Switzerland"},
    "1716": {"make": "Flyability", "company": "Flyability SA", "country": "Switzerland"},
    "1703": {"make": "senseFly / AgEagle", "company": "AgEagle Aerial Systems / senseFly", "country": "Switzerland / USA"},
    "1714": {"make": "Dronetag", "company": "Dronetag s.r.o.", "country": "Czech Republic"},
    "1588": {"make": "Intel", "company": "Intel Corporation", "country": "United States"},
    "1623": {"make": "Freefly Systems", "company": "Freefly Systems Inc.", "country": "United States"},
    "1730": {"make": "Blueflite", "company": "Blueflite, Inc.", "country": "United States"},
    "1797": {"make": "CubePilot / Hex", "company": "Hex Technology / CubePilot", "country": "Hong Kong / Australia"},
    "1824": {"make": "ArduPilot / Custom DRI", "company": "Open Source / DIY Broadcast Module", "country": "Global"},
    "1867": {"make": "Elistair", "company": "Elistair SAS (Tethered Drones)", "country": "France"},
    "1890": {"make": "Quantum Systems", "company": "Quantum-Systems GmbH", "country": "Germany"},
    "1900": {"make": "Volocopter", "company": "Volocopter GmbH", "country": "Germany"},
    "1914": {"make": "XAG", "company": "XAG Co., Ltd. (Agriculture)", "country": "China"},
    "1610": {"make": "JOUAV", "company": "JOUAV Automation Tech Co., Ltd.", "country": "China"},
    "1642": {"make": "AeroVironment", "company": "AeroVironment, Inc.", "country": "United States"},
    "1675": {"make": "Harris Aerial", "company": "Harris Aerial LLC", "country": "United States"},
    "1680": {"make": "Inspired Flight", "company": "Inspired Flight Technologies", "country": "United States"},
    "1738": {"make": "Sony", "company": "Sony Group Corporation (Airpeak)", "country": "Japan"},
    "1755": {"make": "Anzu Robotics", "company": "Anzu Robotics LLC", "country": "United States"},
    "1772": {"make": "Teal Drones", "company": "Teal Drones (Red Cat Holdings)", "country": "United States"},
    "1790": {"make": "BRINC Drones", "company": "BRINC Drones Inc.", "country": "United States"},
}

# Sub-model Hardware Generation Prefixes (chars 1-6 or 1-7)
MODEL_PREFIX_MAP: Dict[str, Dict[str, str]] = {
    # --- DJI Models (MFR: 1581, Length Code: F) ---
    "1581F4": {"make": "DJI", "model": "Mini 3 / Mini 3 Pro Series"},
    "1581F5": {"make": "DJI", "model": "Mavic 3 / Classic / Pro Series"},
    "1581F6": {"make": "DJI", "model": "Avata / FPV Series"},
    "1581F7": {"make": "DJI", "model": "Inspire 3 / Ronin 4D"},
    "1581F8": {"make": "DJI", "model": "Matrice 30 / 4TD / 350 RTK Enterprise"},
    "1581F9": {"make": "DJI", "model": "Mini 4 Pro / Air 3 Series"},
    "1581FA": {"make": "DJI", "model": "Agras T40 / T50 / T25 (Agriculture)"},
    "1581FB": {"make": "DJI", "model": "Avata 2 Series"},
    "1581FC": {"make": "DJI", "model": "Air 3S / Neo Series"},
    "1581FD": {"make": "DJI", "model": "Matrice 3D / 3TD Dock Series"},
    "1581FE": {"make": "DJI", "model": "Matrice 400 / Enterprise Next-Gen"},
    "1581FF": {"make": "DJI", "model": "DJI Enterprise / Flight Hub Module"},

    # --- Autel Robotics Models (MFR: 1596, Length Code: E) ---
    "1596E1": {"make": "Autel Robotics", "model": "EVO II Pro / Dual 640T Series"},
    "1596E2": {"make": "Autel Robotics", "model": "EVO Lite+ / Nano+ Series"},
    "1596E3": {"make": "Autel Robotics", "model": "EVO Max 4T / 4N Series"},
    "1596E4": {"make": "Autel Robotics", "model": "Dragonfish VTOL Series"},
    "1596E5": {"make": "Autel Robotics", "model": "Autel Alpha / Titan Series"},

    # --- Skydio Models (MFR: 1668) ---
    "1668": {"make": "Skydio", "model": "Skydio 2+ / X2 / X10 Series"},

    # --- Parrot Models (MFR: 1748) ---
    "1748": {"make": "Parrot", "model": "ANAFI / ANAFI USA / ANAFI Ai Series"},

    # --- Dronetag Broadcast Modules (MFR: 1714) ---
    "1714": {"make": "Dronetag", "model": "Dronetag Mini / Beacon / BS / DRI Module"},

    # --- Wingtra (MFR: 1686) ---
    "1686": {"make": "Wingtra", "model": "WingtraOne GEN II VTOL"},

    # --- Flyability (MFR: 1716) ---
    "1716": {"make": "Flyability", "model": "Elios 3 Confined Space Drone"},
}


def infer_drone_model(serial_number: Optional[str]) -> Dict[str, Any]:
    """
    Infers drone make, model family, manufacturer company, and country of origin
    from an ANSI/CTA-2063-A compliant serial number string.

    Returns:
        Dict containing:
            - make (str or None): Short brand name (e.g. "DJI", "Autel Robotics")
            - model (str or None): Inferred model family (e.g. "Matrice 30 / 4TD / 350 RTK Enterprise")
            - mfr_code (str or None): 4-character CTA manufacturer code (e.g. "1581")
            - company (str or None): Full legal manufacturer name
            - country (str or None): Manufacturer country of registration
            - is_inferred (bool): True if at least make was identified
    """
    if not serial_number or not isinstance(serial_number, str):
        return {
            "make": None,
            "model": None,
            "mfr_code": None,
            "company": None,
            "country": None,
            "is_inferred": False,
        }

    clean = serial_number.strip().upper()
    if len(clean) < 4:
        return {
            "make": None,
            "model": None,
            "mfr_code": None,
            "company": None,
            "country": None,
            "is_inferred": False,
        }

    # 1. Check Sub-model prefix matches (first 6 chars, then 5, then 4)
    for length in (6, 5, 4):
        if len(clean) >= length:
            sub_prefix = clean[:length]
            if sub_prefix in MODEL_PREFIX_MAP:
                mapping = MODEL_PREFIX_MAP[sub_prefix]
                mfr_code = clean[:4]
                mfr_info = CTA_MANUFACTURERS.get(mfr_code, {})
                return {
                    "make": mapping.get("make") or mfr_info.get("make"),
                    "model": mapping.get("model"),
                    "mfr_code": mfr_code,
                    "company": mfr_info.get("company"),
                    "country": mfr_info.get("country"),
                    "is_inferred": True,
                }

    # 2. Check 4-character MFR code match
    mfr_code = clean[:4]
    if mfr_code in CTA_MANUFACTURERS:
        mfr_info = CTA_MANUFACTURERS[mfr_code]
        return {
            "make": mfr_info.get("make"),
            "model": f"{mfr_info.get('make')} Aircraft",
            "mfr_code": mfr_code,
            "company": mfr_info.get("company"),
            "country": mfr_info.get("country"),
            "is_inferred": True,
        }

    return {
        "make": None,
        "model": None,
        "mfr_code": None,
        "company": None,
        "country": None,
        "is_inferred": False,
    }
