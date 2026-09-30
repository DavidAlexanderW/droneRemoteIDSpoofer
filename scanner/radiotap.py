#!/usr/bin/env python3
"""
IEEE 802.11 Radiotap Header Parser
Extracts physical layer RF parameters (RSSI, data rate, modulation, frequency,
channel flags, and 802.11n HT MCS parameters) strictly adhering to the
Radiotap natural alignment specification.
"""

import struct
from typing import Any, Dict, Optional


def extract_radiotap_phy_info(frame: bytes) -> Dict[str, Any]:
    """
    Extracts physical layer RF parameters (RSSI, data rate, modulation, frequency, channel flags,
    and 802.11n HT MCS parameters) from an IEEE 802.11 Radiotap header.
    Adheres strictly to the IEEE 802.11 Radiotap natural alignment specification.
    """
    res: Dict[str, Any] = {
        "rssi_dbm": None,
        "rate_mbps": None,
        "modulation": None,
        "rate_desc": None,
        "frequency_mhz": None,
        "channel_flags": None,
        "mcs_index": None,
        "bandwidth_mhz": None,
        "guard_interval": None,
    }
    if len(frame) < 8 or frame[0] != 0x00:
        return res

    try:
        radiotap_len = struct.unpack('<H', frame[2:4])[0]
        if len(frame) < radiotap_len or radiotap_len < 8:
            return res

        # 1. Parse present bitmasks (each 4 bytes; bit 31 indicates another word follows)
        present_words = []
        idx = 4
        while idx + 4 <= radiotap_len:
            present = struct.unpack('<I', frame[idx:idx+4])[0]
            present_words.append(present)
            idx += 4
            if not (present & 0x80000000):
                break

        if not present_words:
            return res

        w0 = present_words[0]
        offset = idx

        # Bit 0: TSFT (8 bytes, 8-byte aligned)
        if w0 & (1 << 0):
            offset = (offset + 7) & ~7
            offset += 8

        # Bit 1: Flags (1 byte, 1-byte aligned)
        if w0 & (1 << 1):
            offset += 1

        # Bit 2: Rate (1 byte, 1-byte aligned, units of 500 kbps)
        raw_rate = None
        if w0 & (1 << 2):
            if offset < radiotap_len and offset < len(frame):
                raw_rate = frame[offset]
                res["rate_mbps"] = round(raw_rate * 0.5, 1)
            offset += 1

        # Bit 3: Channel (4 bytes: 2B freq + 2B flags, 2-byte aligned)
        if w0 & (1 << 3):
            offset = (offset + 1) & ~1
            if offset + 4 <= radiotap_len and offset + 4 <= len(frame):
                freq, ch_flags = struct.unpack('<HH', frame[offset:offset+4])
                res["frequency_mhz"] = int(freq)
                res["channel_flags"] = int(ch_flags)
            offset += 4

        # Bit 4: FHSS (2 bytes, 2-byte aligned)
        if w0 & (1 << 4):
            offset = (offset + 1) & ~1
            offset += 2

        # Bit 5: dBm Antenna Signal (1 byte signed int8, 1-byte aligned)
        if w0 & (1 << 5):
            if offset < radiotap_len and offset < len(frame):
                val = struct.unpack('<b', frame[offset:offset+1])[0]
                res["rssi_dbm"] = int(val)
            offset += 1

        # Bit 6: dBm Antenna Noise (1 byte signed int8, 1-byte aligned)
        if w0 & (1 << 6):
            offset += 1

        # Bit 7: Lock quality (2 bytes, 2-byte aligned)
        if w0 & (1 << 7):
            offset = (offset + 1) & ~1
            offset += 2

        # Bit 8: TX attenuation (2 bytes, 2-byte aligned)
        if w0 & (1 << 8):
            offset = (offset + 1) & ~1
            offset += 2

        # Bit 9: dB TX attenuation (2 bytes, 2-byte aligned)
        if w0 & (1 << 9):
            offset = (offset + 1) & ~1
            offset += 2

        # Bit 10: dBm TX power (1 byte signed int8, 1-byte aligned)
        if w0 & (1 << 10):
            offset += 1

        # Bit 11: Antenna (1 byte u8, 1-byte aligned)
        if w0 & (1 << 11):
            offset += 1

        # Bit 12: dB Antenna Signal (1 byte u8, 1-byte aligned)
        if w0 & (1 << 12):
            offset += 1

        # Bit 13: dB Antenna Noise (1 byte u8, 1-byte aligned)
        if w0 & (1 << 13):
            offset += 1

        # Bit 14: RX flags (2 bytes u16, 2-byte aligned)
        if w0 & (1 << 14):
            offset = (offset + 1) & ~1
            offset += 2

        # Bit 15: TX flags (2 bytes u16, 2-byte aligned)
        if w0 & (1 << 15):
            offset = (offset + 1) & ~1
            offset += 2

        # Bit 16: RTS retries (1 byte u8, 1-byte aligned)
        if w0 & (1 << 16):
            offset += 1

        # Bit 17: Data retries (1 byte u8, 1-byte aligned)
        if w0 & (1 << 17):
            offset += 1

        # Bit 18: XChannel (8 bytes, 4-byte aligned)
        if w0 & (1 << 18):
            offset = (offset + 3) & ~3
            offset += 8

        # Bit 19: MCS (3 bytes: known, flags, mcs_index; 1-byte aligned)
        if w0 & (1 << 19):
            if offset + 3 <= radiotap_len and offset + 3 <= len(frame):
                known = frame[offset]
                flags = frame[offset + 1]
                mcs = frame[offset + 2]
                res["mcs_index"] = int(mcs)
                bw_flag = flags & 0x03
                res["bandwidth_mhz"] = 40 if bw_flag == 1 else 20
                sgi = bool(flags & 0x04)
                res["guard_interval"] = "Short GI" if sgi else "Long GI"
                res["modulation"] = "HT (802.11n)"
                ht20_lgi = [6.5, 13.0, 19.5, 26.0, 39.0, 52.0, 58.5, 65.0]
                ht20_sgi = [7.2, 14.4, 21.7, 28.9, 43.3, 57.8, 65.0, 72.2]
                ht40_lgi = [13.5, 27.0, 40.5, 54.0, 81.0, 108.0, 121.5, 135.0]
                ht40_sgi = [15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 135.0, 150.0]
                if mcs < 8:
                    if res["bandwidth_mhz"] == 40:
                        res["rate_mbps"] = ht40_sgi[mcs] if sgi else ht40_lgi[mcs]
                    else:
                        res["rate_mbps"] = ht20_sgi[mcs] if sgi else ht20_lgi[mcs]
                gi_str = "SGI" if sgi else "LGI"
                rate_str = f"{res['rate_mbps']:.1f} Mbps " if res["rate_mbps"] else ""
                res["rate_desc"] = f"MCS {mcs} ({rate_str}HT{res['bandwidth_mhz']} {gi_str})".strip()
            offset += 3

        # If legacy rate was found and MCS wasn't present, determine modulation
        if res["rate_mbps"] is not None and res["modulation"] is None:
            r = res["rate_mbps"]
            ch_fl = res["channel_flags"] or 0
            if ch_fl & 0x0040:  # OFDM flag
                res["modulation"] = "OFDM"
            elif ch_fl & 0x0020:  # CCK flag
                res["modulation"] = "DSSS" if r <= 2.0 else "CCK"
            elif r in (1.0, 2.0):
                res["modulation"] = "DSSS"
            elif r in (5.5, 11.0):
                res["modulation"] = "CCK"
            elif r in (6.0, 9.0, 12.0, 18.0, 24.0, 36.0, 48.0, 54.0):
                res["modulation"] = "OFDM"
            else:
                res["modulation"] = "802.11"

            res["rate_desc"] = f"{r:.1f} Mbps {res['modulation']}"

    except Exception:
        pass

    return res


def extract_radiotap_rssi(frame: bytes) -> Optional[int]:
    """
    Extracts the dBm Antenna Signal (RSSI) from an IEEE 802.11 Radiotap header.
    Maintained for backward compatibility; delegates to extract_radiotap_phy_info.
    """
    return extract_radiotap_phy_info(frame).get("rssi_dbm")
