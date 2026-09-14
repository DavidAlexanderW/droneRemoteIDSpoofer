#!/usr/bin/env python3
"""
Combined Bluetooth (nRF UART) and Wi-Fi Drone Remote ID (RID) Listener & Logger
Compliant with ASTM F3411-19 / ASTM F3411-22 / ASD-STAN OpenDroneID standards.

Architecture:
- Thread 1: BLE Sniffer Thread (runs nrf_bt_sniffer_json.py over UART for BLE 4/5 RID packets)
- Thread 2: Wi-Fi Channel Hopper Thread (executes multi-band 2.4 GHz / 5.8 GHz hopping schedule)
- Thread 3: Wi-Fi Sniffer Thread (AF_PACKET raw socket capturing Beacons & NAN Action Frames)
- Logging Pipeline:
    1. Real-time formatted console display with colorized telemetry.
    2. Replay-compatible append-only JSONL log (direct drop-in for replay_drones.py).
    3. SQLite database for 5-minute (300s) aggregated Flight Encounters & trajectory tracking.
"""

import argparse
import base64
import json
import logging
import os
import queue
import select
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ANSI Terminal Colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_MAGENTA = "\033[95m"
C_CYAN = "\033[96m"
C_WHITE = "\033[97m"
C_GRAY = "\033[90m"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("CombinedRIDListener")


# ============================================================================
# ASTM F3411 Protocol Constants & Spec Parser (Imported from drone_rid_spoofer.parser)
# ============================================================================

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from drone_rid_spoofer.parser import (
    ASTM_OUI,
    APP_CODE_RID,
    BLE_RID_UUID,
    OPENDRONEID_EPOCH_2019,
    MSG_TYPE_NAMES,
    PROTO_VERSION_NAMES,
    ID_TYPE_NAMES,
    UA_TYPE_NAMES,
    STATUS_NAMES,
    HEIGHT_TYPE_NAMES,
    HORIZ_ACCURACY_NAMES,
    VERT_ACCURACY_NAMES,
    SPEED_ACCURACY_NAMES,
    TIMESTAMP_ACCURACY_NAMES,
    AUTH_TYPE_NAMES,
    DESC_TYPE_NAMES,
    OPERATOR_LOCATION_TYPE_NAMES,
    CLASSIFICATION_TYPE_NAMES,
    EU_CATEGORY_NAMES,
    EU_CLASS_NAMES,
    sanitize_ascii_string,
    decode_astm_message,
    parse_astm_payload,
)

try:
    from scanner.db import (
        init_encounters_db,
        get_db_connection as db_get_connection,
        reconcile_stale_encounters,
        touch_receiver_node_heartbeat,
        upsert_receiver_node,
    )
    from scanner.drone_models import infer_drone_model
    from scanner.forwarder import CentralStreamForwarder
    from scanner.scanner_config import load_scanner_config
except ImportError:
    try:
        from db import (
            init_encounters_db,
            get_db_connection as db_get_connection,
            reconcile_stale_encounters,
            touch_receiver_node_heartbeat,
            upsert_receiver_node,
        )
        from drone_models import infer_drone_model
        from forwarder import CentralStreamForwarder
        from scanner_config import load_scanner_config
    except ImportError:
        CentralStreamForwarder = None
        load_scanner_config = lambda *args, **kwargs: {}
        def infer_drone_model(serial):
            return {"make": None, "model": None, "company": None, "country": None, "is_inferred": False}


# ============================================================================
# Wi-Fi Channel State & Hopping Definitions
# ============================================================================

SOCIAL_CHANNEL_2G = 6
NON_SOCIAL_CHANNELS_2G = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]

SOCIAL_CHANNEL_5G = 149
NON_SOCIAL_CHANNELS_5G = [153, 157, 161, 165, 169, 173]


def get_band_for_channel(ch: int) -> str:
    if ch <= 14:
        return "2.4GHz"
    elif ch >= 36:
        return "5.8GHz" if ch >= 149 else "5GHz"
    return "Unknown"


def get_freq_for_channel(ch: int) -> int:
    if ch == 14:
        return 2484
    elif 1 <= ch <= 13:
        return 2407 + 5 * ch
    elif ch >= 36:
        return 5000 + 5 * ch
    return 0


def get_channel_for_freq(freq: int) -> int:
    """Calculates 802.11 channel number from center frequency in MHz."""
    if freq == 2484:
        return 14
    elif 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    elif 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    return 0


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


class SharedChannelState:
    """
    Thread-safe state holding the current active Wi-Fi channel, band, and frequency.
    Supports transition-aware packet attribution:
    - Retains previous channel for drain_retention_s (default: 5ms) after switch start.
    - Honors physical radiotap frequency header as ground-truth when available.
    """
    def __init__(self, initial_channel: int = 6, drain_retention_ms: float = 5.0):
        self.lock = threading.Lock()
        self.channel = initial_channel
        self.band = get_band_for_channel(initial_channel)
        self.frequency = get_freq_for_channel(initial_channel)
        self.previous_channel = initial_channel
        self.previous_band = self.band
        self.previous_frequency = self.frequency
        self.target_channel = initial_channel
        self.target_band = self.band
        self.target_frequency = self.frequency
        self.is_switching = False
        self.switch_start_time = 0.0
        self.drain_retention_s = max(0.0, drain_retention_ms / 1000.0)
        self.last_switch_time = time.time()
        self.total_switches = 0

    def start_switch(self, target_channel: int, source_channel: Optional[int] = None):
        """Signals start of physical channel transition."""
        with self.lock:
            src = source_channel if source_channel is not None else self.channel
            self.previous_channel = src
            self.previous_band = get_band_for_channel(src)
            self.previous_frequency = get_freq_for_channel(src)
            self.target_channel = target_channel
            self.target_band = get_band_for_channel(target_channel)
            self.target_frequency = get_freq_for_channel(target_channel)
            self.is_switching = True
            self.switch_start_time = time.time()

    def finish_switch(self, target_channel: int):
        """Signals completion of channel switch command."""
        with self.lock:
            self.channel = target_channel
            self.band = get_band_for_channel(target_channel)
            self.frequency = get_freq_for_channel(target_channel)
            self.target_channel = target_channel
            self.is_switching = False
            self.last_switch_time = time.time()
            self.total_switches += 1

    def update(self, new_channel: int):
        """Direct channel update (backward-compatible)."""
        with self.lock:
            self.previous_channel = self.channel
            self.previous_band = self.band
            self.previous_frequency = self.frequency
            self.channel = new_channel
            self.band = get_band_for_channel(new_channel)
            self.frequency = get_freq_for_channel(new_channel)
            self.target_channel = new_channel
            self.target_band = self.band
            self.target_frequency = self.frequency
            self.is_switching = False
            self.last_switch_time = time.time()
            self.total_switches += 1

    def resolve_channel(self, pkt_ts: float, radiotap_freq: Optional[int] = None) -> Tuple[int, str, int]:
        """
        Resolves the true physical channel, band, and frequency for a captured frame:
        1. If valid radiotap_freq is present, resolves directly from radiotap.
        2. If switching and packet arrived within drain retention window (< 5ms), attributes to previous channel.
        3. If switching and packet arrived after drain window, attributes to target channel.
        4. Otherwise returns the currently active channel.
        """
        if radiotap_freq and radiotap_freq > 0:
            ch = get_channel_for_freq(radiotap_freq)
            if ch > 0:
                return ch, get_band_for_channel(ch), radiotap_freq

        with self.lock:
            if self.is_switching:
                elapsed = pkt_ts - self.switch_start_time
                if elapsed < self.drain_retention_s:
                    return self.previous_channel, self.previous_band, self.previous_frequency
                else:
                    return self.target_channel, self.target_band, self.target_frequency
            return self.channel, self.band, self.frequency

    def get(self) -> Tuple[int, str, int, float]:
        with self.lock:
            return self.channel, self.band, self.frequency, self.last_switch_time


# ============================================================================
# SQLite Encounter Database Manager
# ============================================================================

class EncounterTracker:
    """
    Groups individual Remote ID packets into 5-minute (300s) Flight Encounters
    and commits completed/updated encounters into an SQLite database.
    """
    def __init__(
        self,
        db_path: Optional[str] = "rid_detections.db",
        timeout_s: float = 300.0,
        persist_interval_s: float = 0.0,
        default_node_id: Optional[str] = None,
    ):
        self.db_path = db_path
        self.timeout_s = timeout_s
        self.persist_interval_s = persist_interval_s
        self.default_node_id = default_node_id
        self.active_encounters: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.last_wal_checkpoint = time.time()

        if self.db_path:
            self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            init_encounters_db(conn, timeout_s=self.timeout_s)

    def update_with_packet(self, packet: Dict[str, Any]) -> str:
        """Update or create an active encounter from an incoming packet. Returns encounter_id."""
        mac = packet.get("mac", "UNKNOWN")
        serial = packet.get("serial_number") or packet.get("serial")
        node_id = packet.get("node_id") or packet.get("primary_node_id") or self.default_node_id
        
        # Robust timestamp resolution
        ts = packet.get("timestamp") or packet.get("timestamp_epoch")
        if ts is None and packet.get("timestamp_iso"):
            try:
                dt = datetime.fromisoformat(packet["timestamp_iso"].replace("Z", "+00:00"))
                ts = dt.timestamp()
            except Exception:
                ts = time.time()
        if ts is None:
            ts = time.time()

        transport = packet.get("transport", "unknown")
        ch_raw = packet.get("channel", "N/A")
        if transport in ("bt4", "bt5"):
            ch_str = f"BLE Ch {ch_raw}" if (isinstance(ch_raw, int) or (isinstance(ch_raw, str) and ch_raw.isdigit())) else str(ch_raw)
        elif transport in ("wifi", "nan"):
            ch_str = f"Wi-Fi Ch {ch_raw}" if (isinstance(ch_raw, int) or (isinstance(ch_raw, str) and ch_raw.isdigit())) else str(ch_raw)
        else:
            ch_str = str(ch_raw)
        rssi = packet.get("rssi_dbm")
        if rssi is None:
            rssi = packet.get("rssi")
        if packet.get("rssi_dbm_invalid"):
            rssi = None
        counter = packet.get("counter")
        if counter is None:
            counter = packet.get("msg_counter")

        # Group encounters by transmitter MAC address within the 5-minute flight window
        key = mac
        now = time.time()

        with self.lock:
            # Check if existing encounter timed out (> 5 minutes), belongs to different encounter_id, or has backward time jump
            if key in self.active_encounters:
                enc = self.active_encounters[key]
                pkt_enc_id = packet.get("encounter_id")
                if (pkt_enc_id and pkt_enc_id != enc["encounter_id"]) or (ts - enc["last_seen"] > self.timeout_s) or (ts < enc["first_seen"]):
                    # Finalize old encounter
                    enc["is_active"] = 0
                    self._persist_encounter(enc)
                    del self.active_encounters[key]

            if key not in self.active_encounters:
                # Generate unique encounter ID or preserve existing
                enc_slug = mac.replace(":", "")[-6:]
                dt_tag = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d-%H%M%S")
                encounter_id = packet.get("encounter_id") or f"ENC-{dt_tag}-{enc_slug}"

                drone_info = infer_drone_model(serial) if serial else {}
                self.active_encounters[key] = {
                    "encounter_id": encounter_id,
                    "mac": mac,
                    "serial_number": serial,
                    "drone_make": drone_info.get("make"),
                    "drone_model": drone_info.get("model"),
                    "node_id": node_id,
                    "first_seen": ts,
                    "first_seen_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    "last_seen": ts,
                    "last_seen_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    "duration_s": 0.0,
                    "packet_count": 0,
                    "counter": counter,
                    "transports": set([transport]),
                    "channels": set([ch_str]),
                    "wifi_rates": set(),
                    "rate_counts": {},
                    "rssi_values": [rssi] if rssi is not None else [],
                    "altitudes": [],
                    "pressure_altitudes": [],
                    "heights": [],
                    "speeds": [],
                    "vert_speeds": [],
                    "pilot_lat": None,
                    "pilot_lon": None,
                    "pilot_alt_m": None,
                    "area_ceil_m": None,
                    "area_floor_m": None,
                    "operator_id": None,
                    "self_id_desc": None,
                    "trajectory": [],
                    "is_active": 1,
                    "last_persisted": 0.0,
                }

            enc = self.active_encounters[key]
            if counter is not None:
                enc["counter"] = counter
            if node_id and not enc.get("node_id"):
                enc["node_id"] = node_id
            enc["last_seen"] = ts
            enc["last_seen_iso"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            enc["duration_s"] = round(enc["last_seen"] - enc["first_seen"], 2)
            enc["packet_count"] += 1
            enc["transports"].add(transport)
            enc["channels"].add(ch_str)
            r_desc = packet.get("rate_desc")
            if r_desc:
                enc["wifi_rates"].add(r_desc)
                if "rate_counts" not in enc:
                    enc["rate_counts"] = {}
                if r_desc not in enc["rate_counts"]:
                    enc["rate_counts"][r_desc] = {
                        "count": 0,
                        "rate_mbps": packet.get("rate_mbps"),
                        "modulation": packet.get("modulation"),
                    }
                enc["rate_counts"][r_desc]["count"] += 1
            if serial and not enc.get("serial_number"):
                enc["serial_number"] = serial
                if not enc.get("drone_make"):
                    inf = infer_drone_model(serial)
                    enc["drone_make"] = inf.get("make")
                    enc["drone_model"] = inf.get("model")

            if rssi is not None:
                enc["rssi_values"].append(rssi)

            # Resolve messages: decode messages_b64 if messages list is empty
            msgs_to_process = packet.get("messages", [])
            if not msgs_to_process and packet.get("messages_b64"):
                msgs_to_process = []
                import base64
                for b64_str in packet["messages_b64"]:
                    try:
                        raw_b = base64.b64decode(b64_str)
                        dm = decode_astm_message(raw_b)
                        if dm:
                            msgs_to_process.append(dm)
                    except Exception:
                        pass

            # Parse message telemetry fields
            for msg in msgs_to_process:
                m_type = msg.get("type")
                if m_type == "Location":
                    lat = msg.get("lat")
                    lon = msg.get("lon")
                    g_alt = msg.get("geodetic_altitude_m")
                    p_alt = msg.get("pressure_altitude_m")
                    alt = g_alt if g_alt is not None else p_alt
                    h_m = msg.get("height_m")
                    h_type = msg.get("height_type")
                    spd = msg.get("speed_mps")
                    heading = msg.get("direction_deg")
                    v_spd = msg.get("vertical_speed_mps")

                    if g_alt is not None:
                        enc["altitudes"].append(g_alt)
                    elif alt is not None:
                        enc["altitudes"].append(alt)
                    if p_alt is not None:
                        enc["pressure_altitudes"].append(p_alt)
                    if h_m is not None:
                        enc["heights"].append(h_m)
                    if spd is not None:
                        enc["speeds"].append(spd)
                    if v_spd is not None:
                        enc["vert_speeds"].append(v_spd)

                    if lat is not None and lon is not None:
                        # Append 12-element trajectory fix [lat, lon, alt_msl, speed, heading, ts, height_m, height_type, pressure_alt_m, vert_spd, rssi, counter]
                        # Downsample trajectory if stationary or dense (<1s dt and <~1m movement)
                        last_pt = enc["trajectory"][-1] if enc["trajectory"] else None
                        should_record = False
                        if last_pt is None:
                            should_record = True
                        else:
                            dt_pt = ts - last_pt[5]
                            if dt_pt >= 1.0 or abs(lat - last_pt[0]) > 0.00001 or abs(lon - last_pt[1]) > 0.00001:
                                should_record = True
                        if should_record:
                            pt_rssi = rssi if rssi is not None else (enc["rssi_values"][-1] if enc.get("rssi_values") else None)
                            pt_counter = counter if counter is not None else enc.get("counter")
                            enc["trajectory"].append([lat, lon, alt, spd, heading, round(ts, 2), h_m, h_type, p_alt, v_spd, pt_rssi, pt_counter])

                elif m_type == "Basic ID":
                    b_id = msg.get("id")
                    if b_id:
                        if not enc.get("serial_number"):
                            enc["serial_number"] = b_id
                        if not enc.get("drone_make"):
                            inf = infer_drone_model(b_id)
                            enc["drone_make"] = inf.get("make")
                            enc["drone_model"] = inf.get("model")

                elif m_type == "System":
                    if msg.get("pilot_lat") is not None:
                        enc["pilot_lat"] = msg.get("pilot_lat")
                    if msg.get("pilot_lon") is not None:
                        enc["pilot_lon"] = msg.get("pilot_lon")
                    if msg.get("pilot_alt_m") is not None:
                        enc["pilot_alt_m"] = msg.get("pilot_alt_m")
                    if msg.get("area_ceiling_m") is not None:
                        enc["area_ceil_m"] = msg.get("area_ceiling_m")
                    elif msg.get("area_ceil_m") is not None:
                        enc["area_ceil_m"] = msg.get("area_ceil_m")
                    if msg.get("area_floor_m") is not None:
                        enc["area_floor_m"] = msg.get("area_floor_m")

                elif m_type == "Operator ID":
                    op_val = msg.get("operator_id") or msg.get("id")
                    if op_val:
                        enc["operator_id"] = op_val

                elif m_type == "Self-ID":
                    desc_val = msg.get("description") or msg.get("desc")
                    if desc_val:
                        enc["self_id_desc"] = desc_val

            # Persist live progress (throttled to at most once per persist_interval_s or first packet)
            if (
                self.persist_interval_s <= 0.0
                or enc["packet_count"] == 1
                or (ts - enc.get("last_persisted", 0.0) >= self.persist_interval_s)
            ):
                self._persist_encounter(enc)
                enc["last_persisted"] = ts

            return enc["encounter_id"]

    def check_timeouts(self, now: Optional[float] = None) -> List[str]:
        """Check for and close any encounters that have been silent for > timeout_s."""
        if now is None:
            now = time.time()
        closed_ids = []
        with self.lock:
            keys_to_delete = []
            for key, enc in self.active_encounters.items():
                if now - enc["last_seen"] > self.timeout_s:
                    enc["is_active"] = 0
                    self._persist_encounter(enc)
                    closed_ids.append(enc["encounter_id"])
                    keys_to_delete.append(key)
            for k in keys_to_delete:
                del self.active_encounters[k]

            # Periodic WAL checkpoint every 5 minutes
            if now - self.last_wal_checkpoint > 300.0:
                self.last_wal_checkpoint = now
                if self.db_path:
                    try:
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                    except Exception:
                        pass
        return closed_ids

    def finalize_all(self):
        """Mark all active encounters as completed on shutdown."""
        with self.lock:
            for enc in self.active_encounters.values():
                enc["is_active"] = 0
                self._persist_encounter(enc)
            self.active_encounters.clear()

    def _persist_encounter(self, enc: Dict[str, Any]):
        if not self.db_path:
            return

        rssi_vals = enc["rssi_values"]
        min_rssi = min(rssi_vals) if rssi_vals else None
        max_rssi = max(rssi_vals) if rssi_vals else None
        avg_rssi = round(sum(rssi_vals) / len(rssi_vals), 1) if rssi_vals else None

        alts = enc.get("altitudes", [])
        min_alt = min(alts) if alts else None
        max_alt = max(alts) if alts else None

        heights = enc.get("heights", [])
        min_height = min(heights) if heights else None
        max_height = max(heights) if heights else None

        p_alts = enc.get("pressure_altitudes", [])
        min_p_alt = min(p_alts) if p_alts else None
        max_p_alt = max(p_alts) if p_alts else None

        speeds = enc.get("speeds", [])
        max_speed = max(speeds) if speeds else None

        transports_str = ",".join(sorted(enc["transports"]))
        channels_str = ",".join(sorted(enc["channels"]))

        # Compute structured PHY rate metrics and JSON distribution
        rate_counts = enc.get("rate_counts", {})
        dominant_rate_mbps = None
        dominant_modulation = None
        min_rate_mbps = None
        max_rate_mbps = None
        phy_dist = {}
        wifi_rates_list = []

        if rate_counts:
            total_phy_pkts = sum(v["count"] for v in rate_counts.values())
            rates = [v["rate_mbps"] for v in rate_counts.values() if v.get("rate_mbps") is not None]
            if rates:
                min_rate_mbps = min(rates)
                max_rate_mbps = max(rates)

            # Sort entries by packet count descending
            sorted_entries = sorted(rate_counts.items(), key=lambda x: x[1]["count"], reverse=True)
            dom_k, dom_v = sorted_entries[0]
            dominant_rate_mbps = dom_v.get("rate_mbps")
            dominant_modulation = dom_v.get("modulation")

            for desc, info in sorted_entries:
                cnt = info["count"]
                pct = round((cnt / total_phy_pkts) * 100.0, 1) if total_phy_pkts > 0 else 0.0
                phy_dist[desc] = {
                    "count": cnt,
                    "rate_mbps": info.get("rate_mbps"),
                    "modulation": info.get("modulation"),
                    "percent": pct,
                }
                if len(sorted_entries) > 1:
                    wifi_rates_list.append(f"{desc} ({pct:.0f}%)")
                else:
                    wifi_rates_list.append(desc)

        phy_rate_dist_json = json.dumps(phy_dist) if phy_dist else None
        wifi_rates_str = ", ".join(wifi_rates_list) if wifi_rates_list else None
        trajectory_str = json.dumps(enc["trajectory"])

        persisted_is_active = 0 if (time.time() - enc["last_seen"] > self.timeout_s) else enc["is_active"]

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO encounters (
                        encounter_id, mac, serial_number, first_seen, first_seen_iso,
                        last_seen, last_seen_iso, duration_s, packet_count, transports,
                        channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps,
                        max_rate_mbps, phy_rate_dist_json, min_rssi_dbm, max_rssi_dbm, avg_rssi_dbm, min_alt_m,
                        max_alt_m, min_height_m, max_height_m, min_pressure_alt_m, max_pressure_alt_m,
                        max_speed_mps, pilot_lat, pilot_lon, pilot_alt_m, area_ceil_m, area_floor_m,
                        operator_id, self_id_desc, drone_make, drone_model, node_id, trajectory_json, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    enc["encounter_id"],
                    enc["mac"],
                    enc["serial_number"],
                    enc["first_seen"],
                    enc["first_seen_iso"],
                    enc["last_seen"],
                    enc["last_seen_iso"],
                    enc["duration_s"],
                    enc["packet_count"],
                    transports_str,
                    channels_str,
                    wifi_rates_str,
                    dominant_rate_mbps,
                    dominant_modulation,
                    min_rate_mbps,
                    max_rate_mbps,
                    phy_rate_dist_json,
                    min_rssi,
                    max_rssi,
                    avg_rssi,
                    min_alt,
                    max_alt,
                    min_height,
                    max_height,
                    min_p_alt,
                    max_p_alt,
                    max_speed,
                    enc["pilot_lat"],
                    enc["pilot_lon"],
                    enc["pilot_alt_m"],
                    enc.get("area_ceil_m"),
                    enc.get("area_floor_m"),
                    enc["operator_id"],
                    enc["self_id_desc"],
                    enc.get("drone_make"),
                    enc.get("drone_model"),
                    enc.get("node_id"),
                    trajectory_str,
                    enc["is_active"]
                ))
                conn.commit()
        except Exception as e:
            logger.debug(f"Error persisting encounter to SQLite: {e}")


def rehydrate_db_from_jsonl(
    db_path: str = "rid_detections.db",
    log_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> int:
    """
    Retroactively parses all raw base64 ASTM messages in JSONL log files (rid_packets_*.jsonl)
    and re-populates/upgrades the SQLite encounter database with full 10-element trajectory fixes,
    min/max heights, pressure altitudes, system telemetry limits, and node_id attribution.
    """
    import glob
    import base64

    if node_id is None:
        try:
            cfg = load_scanner_config() if load_scanner_config else {}
            node_id = cfg.get("node_id", "sensor-node-01")
        except Exception:
            node_id = "sensor-node-01"

    if log_dir is None:
        search_patterns = [
            "rid_packets_*.jsonl",
            "rid_packets*.jsonl",
            "central_logs/rid_packets*.jsonl",
            "central_logs/*.jsonl",
            os.path.join(repo_root, "rid_packets*.jsonl"),
            os.path.join(repo_root, "central_logs", "rid_packets*.jsonl"),
            os.path.join(repo_root, "central_logs", "*.jsonl"),
            os.path.join(os.path.dirname(__file__), "..", "rid_packets*.jsonl"),
            os.path.join(os.path.dirname(__file__), "..", "central_logs", "rid_packets*.jsonl"),
            os.path.join(os.path.dirname(__file__), "rid_packets*.jsonl"),
        ]
    else:
        search_patterns = [os.path.join(log_dir, "rid_packets_*.jsonl"), os.path.join(log_dir, "*.jsonl")]

    if db_path == "rid_detections.db":
        if os.path.isfile("rid_detections_central.db") and not os.path.isfile("rid_detections.db"):
            db_path = "rid_detections_central.db"
        elif os.path.isfile(os.path.join(repo_root, "rid_detections_central.db")) and not os.path.isfile(os.path.join(repo_root, "rid_detections.db")):
            db_path = os.path.join(repo_root, "rid_detections_central.db")

    log_files = []
    for pattern in search_patterns:
        for p in glob.glob(pattern):
            abs_p = os.path.abspath(p)
            if abs_p not in log_files and os.path.isfile(abs_p):
                log_files.append(abs_p)

    if not log_files:
        logger.info("[*] Rehydration: No JSONL packet log files found.")
        return 0

    logger.info(f"[*] Rehydration: Found {len(log_files)} packet log file(s): {[os.path.basename(f) for f in log_files]}")

    all_packets: List[Dict[str, Any]] = []
    for fpath in sorted(log_files):
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        rec = json.loads(line)
                        all_packets.append(rec)
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Error reading {fpath} for rehydration: {e}")

    if not all_packets:
        logger.info("[*] Rehydration: No valid packet records found in JSONL logs.")
        return 0

    def _resolve_ts(p: Dict[str, Any]) -> float:
        ts = p.get("timestamp") or p.get("timestamp_epoch")
        if ts is None and p.get("timestamp_iso"):
            try:
                dt = datetime.fromisoformat(p["timestamp_iso"].replace("Z", "+00:00"))
                ts = dt.timestamp()
            except Exception:
                pass
        return float(ts) if ts is not None else 0.0

    # Sort all packets chronologically so encounters build accurately over time
    all_packets.sort(key=_resolve_ts)

    try:
        tracker = EncounterTracker(db_path=db_path, persist_interval_s=0.0, default_node_id=node_id)
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower() or "permission" in str(e).lower():
            logger.error(f"[-] Database permission error opening '{db_path}': {e}\n"
                         f"    -> The database was likely created by root/sudo. Run rehydration with sudo:\n"
                         f"       sudo .venv/bin/python3 scanner/combined_rid_listener.py --rehydrate\n"
                         f"    -> Or fix file ownership with:\n"
                         f"       sudo chown -R $USER:$USER .\n")
            return 0
        raise

    for p in all_packets:
        decoded_msgs = p.get("messages", [])
        if not decoded_msgs and p.get("messages_b64"):
            try:
                raw_blocks = [base64.b64decode(b) for b in p["messages_b64"]]
                if parse_astm_payload:
                    parsed_msgs, _ = parse_astm_payload(b"".join(raw_blocks))
                    decoded_msgs = parsed_msgs
                elif decode_astm_message:
                    decoded_msgs = [decode_astm_message(b) for b in raw_blocks if decode_astm_message(b)]
            except Exception:
                pass

        ts = _resolve_ts(p)
        if ts <= 0.0:
            ts = time.time()

        mac = p.get("mac", "UNKNOWN")
        serial = p.get("serial_number") or p.get("serial")
        pkt_node_id = p.get("node_id") or node_id

        pkt_rssi = p.get("rssi_dbm")
        if pkt_rssi is None:
            pkt_rssi = p.get("rssi")
        if p.get("rssi_dbm_invalid"):
            pkt_rssi = None

        pkt_obj = {
            "timestamp": ts,
            "transport": p.get("transport", "wifi"),
            "channel": p.get("channel", "N/A"),
            "mac": mac,
            "node_id": pkt_node_id,
            "rssi_dbm": pkt_rssi,
            "counter": p.get("counter") if p.get("counter") is not None else p.get("msg_counter"),
            "rate_desc": p.get("rate_desc"),
            "rate_mbps": p.get("rate_mbps"),
            "modulation": p.get("modulation"),
            "serial_number": serial,
            "encounter_id": p.get("encounter_id"),
            "messages": decoded_msgs,
        }
        tracker.update_with_packet(pkt_obj)

    tracker.finalize_all()

    # Query count of encounters in DB
    rehydrated_count = 0
    try:
        with sqlite3.connect(db_path) as conn:
            rehydrated_count = conn.execute("SELECT COUNT(*) FROM encounters;").fetchone()[0]
    except Exception:
        pass

    logger.info(f"[+] Rehydration complete: {rehydrated_count} encounter(s) updated in {db_path}")
    return rehydrated_count


# ============================================================================
# Thread 1: Wi-Fi Channel Hopper Thread
# ============================================================================

class WifiChannelHopperThread(threading.Thread):
    """
    Dedicated thread executing the Wi-Fi Remote ID channel hopping schedule:
    - 2.4 GHz Social Channel: Ch 6 (1000 ms dwell)
    - 2.4 GHz Non-Social Channels: (200 ms dwell each)
    - 5.8 GHz Social Channel: Ch 149 (1000 ms dwell)
    - 5.8 GHz Non-Social Channels: (extended 250 ms dwell to compensate for mixed/negative PLL lead times)
    - Configurable non-social ratio (2k on 2.4GHz for every k on 5.8GHz)
    - Transition-Aware Packet Attribution: Packets arriving within the drain retention
      window (< 5ms) are attributed to the previous channel; all subsequent packets
      received during and after the switch are attributed to the target channel.
    """
    def __init__(
        self,
        interface: str,
        channel_state: SharedChannelState,
        non_social_ratio_k: int = 1,
        social_dwell_ms: int = 1000,
        non_social_dwell_ms: int = 200,
        non_social_dwell_5g_ms: Optional[int] = 250,
    ):
        super().__init__(name="WifiHopperThread", daemon=True)
        self.interface = interface
        self.channel_state = channel_state
        self.k = max(1, non_social_ratio_k)
        self.social_dwell_s = social_dwell_ms / 1000.0
        self.non_social_dwell_2g_s = non_social_dwell_ms / 1000.0
        self.non_social_dwell_5g_s = (non_social_dwell_5g_ms if non_social_dwell_5g_ms is not None else max(non_social_dwell_ms, 250)) / 1000.0
        self.running = False
        self.current_channel = 6

        self.n_2g_non_social = 2 * self.k
        self.n_5g_non_social = self.k

        self.idx_2g = 0
        self.idx_5g = 0

    def _set_channel(self, channel: int) -> bool:
        # Method 1: iw dev <iface> set channel <ch>
        try:
            res = subprocess.run(
                ["iw", "dev", self.interface, "set", "channel", str(channel)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass

        # Method 2: iwconfig <iface> channel <ch>
        try:
            res = subprocess.run(
                ["iwconfig", self.interface, "channel", str(channel)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass

        # Method 3: iw dev <iface> set freq <freq_mhz>
        freq = get_freq_for_channel(channel)
        if freq > 0:
            try:
                res = subprocess.run(
                    ["iw", "dev", self.interface, "set", "freq", str(freq)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
                )
                if res.returncode == 0:
                    return True
            except Exception:
                pass

        return False

    def _hop_step(self, target_channel: int, target_dwell_s: float):
        if not self.running:
            return

        self.channel_state.start_switch(target_channel, source_channel=self.current_channel)
        success = self._set_channel(target_channel)
        if success:
            self.current_channel = target_channel
        self.channel_state.finish_switch(target_channel)

        if not self.running:
            return

        time.sleep(target_dwell_s)

    def run(self):
        self.running = True
        logger.info(
            f"[*] Wi-Fi Hopper started on {self.interface} "
            f"(2.4G Non-Social: {self.n_2g_non_social}/cycle, 5.8G Non-Social: {self.n_5g_non_social}/cycle, "
            f"Social Dwell: {self.social_dwell_s*1000:.0f}ms, 2.4G Non-Social: {self.non_social_dwell_2g_s*1000:.0f}ms, "
            f"5.8G Non-Social: {self.non_social_dwell_5g_s*1000:.0f}ms)"
        )

        self._set_channel(SOCIAL_CHANNEL_2G)
        self.channel_state.update(SOCIAL_CHANNEL_2G)

        try:
            while self.running:
                # --- Step 1: 2.4 GHz Social Channel (Ch 6) #1 ---
                self._hop_step(SOCIAL_CHANNEL_2G, self.social_dwell_s)

                # --- Step 2: 2.4 GHz Non-Social Channels (2k channels) ---
                for _ in range(self.n_2g_non_social):
                    if not self.running:
                        break
                    ch_2g = NON_SOCIAL_CHANNELS_2G[self.idx_2g % len(NON_SOCIAL_CHANNELS_2G)]
                    self.idx_2g += 1
                    self._hop_step(ch_2g, self.non_social_dwell_2g_s)

                # --- Step 3: 2.4 GHz Social Channel (Ch 6) #2 (Priority Channel 6 Return) ---
                self._hop_step(SOCIAL_CHANNEL_2G, self.social_dwell_s)

                # --- Step 4: 5.8 GHz Social Channel (Ch 149) ---
                self._hop_step(SOCIAL_CHANNEL_5G, self.social_dwell_s)

                # --- Step 5: 5.8 GHz Non-Social Channels (k channels) ---
                for _ in range(self.n_5g_non_social):
                    if not self.running:
                        break
                    ch_5g = NON_SOCIAL_CHANNELS_5G[self.idx_5g % len(NON_SOCIAL_CHANNELS_5G)]
                    self.idx_5g += 1
                    self._hop_step(ch_5g, self.non_social_dwell_5g_s)

        except Exception as e:
            if self.running:
                logger.error(f"[-] Wi-Fi Hopper encountered error: {e}")
        finally:
            self.running = False
            logger.info("[*] Wi-Fi Channel Hopper stopped.")

    def stop(self):
        self.running = False


# ============================================================================
# Thread 2: Wi-Fi Sniffer & Frame Parser Thread
# ============================================================================

class WifiSnifferThread(threading.Thread):
    """
    Captures raw 802.11 frames on monitor-mode interface using AF_PACKET raw socket.
    Parses Wi-Fi Beacon Vendor Specific Elements (FA:0B:BC) and NAN Action frames.
    """
    def __init__(
        self,
        interface: str,
        channel_state: SharedChannelState,
        event_queue: queue.Queue,
    ):
        super().__init__(name="WifiSnifferThread", daemon=True)
        self.interface = interface
        self.channel_state = channel_state
        self.event_queue = event_queue
        self.running = False
        self.sock: Optional[socket.socket] = None

    def run(self):
        self.running = True
        logger.info(f"[*] Starting Wi-Fi Sniffer on {self.interface}...")

        try:
            self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
            self.sock.bind((self.interface, 0))
        except Exception as e:
            logger.error(f"[-] Failed to bind AF_PACKET raw socket on {self.interface}: {e}. (Need root / sudo)")
            self.running = False
            return

        while self.running:
            try:
                ready = select.select([self.sock], [], [], 0.1)
                if not ready[0]:
                    continue

                frame = self.sock.recv(4096)
                if len(frame) < 24:
                    continue

                ts = time.time()

                # Fast check for ASTM OUI (FA:0B:BC) in Vendor Specific IEs (0xDD) or NAN Action frames
                vendor_ie_idx = -1
                search_offset = 0
                while True:
                    idx = frame.find(ASTM_OUI, search_offset)
                    if idx == -1:
                        break
                    if idx >= 2 and frame[idx - 2] == 0xDD:
                        vendor_ie_idx = idx
                        break
                    search_offset = idx + 1

                is_nan = False
                radiotap_len = struct.unpack('<H', frame[2:4])[0] if (len(frame) >= 4 and frame[0] == 0x00) else 0

                if vendor_ie_idx == -1:
                    # Check for NAN Action frames (0xD0 / 0xE0)
                    if len(frame) > radiotap_len + 24:
                        fc = frame[radiotap_len]
                        if fc in (0xD0, 0xE0):
                            for offset in range(radiotap_len + 24, min(len(frame) - 4, radiotap_len + 128)):
                                if (frame[offset] >> 4) == 0xF and frame[offset + 1] == 0x19:
                                    is_nan = True
                                    nan_pack_offset = offset
                                    break
                    if not is_nan:
                        continue

                if len(frame) < radiotap_len + 24:
                    continue

                # Extract MAC Address (addr2 / transmitter address at offset radiotap_len + 10)
                mac_bytes = frame[radiotap_len + 10 : radiotap_len + 16]
                mac_addr = ':'.join(f'{b:02X}' for b in mac_bytes)

                # Extract PHY RF parameters (RSSI, data rate, modulation, channel freq)
                phy_info = extract_radiotap_phy_info(frame)
                rssi_dbm = phy_info.get("rssi_dbm")
                rate_mbps = phy_info.get("rate_mbps")
                modulation = phy_info.get("modulation")
                rate_desc = phy_info.get("rate_desc")
                bandwidth_mhz = phy_info.get("bandwidth_mhz")
                mcs_index = phy_info.get("mcs_index")
                guard_interval = phy_info.get("guard_interval")
                cur_ch, cur_band, cur_freq = self.channel_state.resolve_channel(ts, phy_info.get("frequency_mhz"))

                # Extract Payload
                counter = 0
                if is_nan:
                    transport = "nan"
                    astm_payload = frame[nan_pack_offset:]
                else:
                    transport = "wifi"
                    ie_len = frame[vendor_ie_idx - 1]
                    if vendor_ie_idx + ie_len > len(frame):
                        continue
                    vendor_data = frame[vendor_ie_idx + 3 : vendor_ie_idx + ie_len]
                    if len(vendor_data) >= 2 and vendor_data[0] == APP_CODE_RID:
                        counter = vendor_data[1]
                        astm_payload = vendor_data[2:]
                    else:
                        counter = 0
                        astm_payload = vendor_data

                # Decode ASTM Messages and raw 25-byte blocks
                parsed_messages, messages_b64 = parse_astm_payload(astm_payload)
                if not parsed_messages:
                    continue

                serial_no = None
                for msg in parsed_messages:
                    if msg.get("type") == "Basic ID" and msg.get("id"):
                        serial_no = msg["id"]
                        break

                event: Dict[str, Any] = {
                    "timestamp": ts,
                    "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    "transport": transport,
                    "interface": self.interface,
                    "channel": cur_ch,
                    "band": cur_band,
                    "frequency_mhz": cur_freq,
                    "rate_mbps": rate_mbps,
                    "modulation": modulation,
                    "rate_desc": rate_desc,
                    "bandwidth_mhz": bandwidth_mhz,
                    "mcs_index": mcs_index,
                    "guard_interval": guard_interval,
                    "counter": counter,
                    "mac": mac_addr,
                    "rssi_dbm": rssi_dbm,
                    "serial_number": serial_no,
                    "messages": parsed_messages,
                    "messages_b64": messages_b64,
                    "raw_length": len(frame),
                }

                self.event_queue.put(event)

            except Exception as e:
                if self.running:
                    logger.debug(f"Error processing Wi-Fi frame: {e}")

        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        logger.info("[*] Wi-Fi Sniffer stopped.")

    def stop(self):
        self.running = False


# ============================================================================
# Thread 3: BLE Sniffer Thread (nRF Sniffer over UART)
# ============================================================================

class BleNrfSnifferThread(threading.Thread):
    """
    Drives nrf_bt_sniffer_json.py as an autonomous background sniffer subprocess over UART.
    Reads structured JSON records from stdout, normalizes them, and feeds the event queue.
    """
    def __init__(
        self,
        event_queue: queue.Queue,
        nrf_port: Optional[str] = None,
        rx_pcap: Optional[str] = None,
        coded: bool = False,
        ble_mode: str = "hop",
        bt5_dwell_s: float = 5.0,
        bt4_dwell_s: float = 1.0,
    ):
        super().__init__(name="BleNrfSnifferThread", daemon=True)
        self.event_queue = event_queue
        self.nrf_port = nrf_port
        self.rx_pcap = rx_pcap
        self.coded = coded
        self.ble_mode = ble_mode
        self.bt5_dwell_s = bt5_dwell_s
        self.bt4_dwell_s = bt4_dwell_s
        self.running = False
        self.proc: Optional[subprocess.Popen] = None

    def stop(self):
        self.running = False
        self.stop_process()

    def run(self):
        self.running = True
        logger.info(f"[*] Starting BLE nRF Sniffer worker thread (Mode: {self.ble_mode}, BT5: {self.bt5_dwell_s}s, BT4: {self.bt4_dwell_s}s)...")

        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate_paths = [
            os.path.join(script_dir, "sniffers", "nrf_bt_sniffer_json.py"),
            os.path.join(script_dir, "nrf_bt_sniffer_json.py"),
            os.path.join(script_dir, "..", "evaluation", "nrf_bt_sniffer_json.py"),
            os.path.join(script_dir, "..", "nrf_bt_sniffer_json.py"),
        ]
        nrf_script = next((p for p in candidate_paths if os.path.exists(p)), candidate_paths[0])

        while self.running:
            # 1. Detect or verify UART serial port if live
            active_port = self.nrf_port
            if not active_port and not self.rx_pcap:
                for candidate in ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2", "/dev/ttyUSB0", "/dev/ttyUSB1"]:
                    if os.path.exists(candidate):
                        active_port = candidate
                        break

            if not active_port and not self.rx_pcap:
                logger.warning("[!] No nRF BLE sniffer device found on /dev/ttyACM* or /dev/ttyUSB*. Retrying in 3s...")
                time.sleep(3.0)
                continue

            # Kill any lingering nrfutil / sniffer instances
            subprocess.run(["killall", "-9", "nrfutil", "nrfutil-ble-sniffer", "nrfutil-ble-sni"],
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            time.sleep(0.3)

            cmd = [
                sys.executable, nrf_script,
                "--only-rid",
                "--ble-mode", self.ble_mode,
                "--bt5-dwell", str(self.bt5_dwell_s),
                "--bt4-dwell", str(self.bt4_dwell_s),
            ]
            if self.coded:
                cmd.append("--coded")
            if active_port:
                cmd.extend(["--nrf-port", active_port])
            elif self.rx_pcap and os.path.exists(self.rx_pcap):
                cmd.extend(["--rx-pcap", self.rx_pcap])

            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, start_new_session=True)
                port_desc = active_port if active_port else self.rx_pcap
                logger.info(f"[*] BLE nRF Sniffer process active on {port_desc}.")

                while self.running and self.proc.poll() is None:
                    line = self.proc.stdout.readline()
                    if not line:
                        continue

                    line_str = line.strip()
                    if not line_str.startswith("{"):
                        continue

                    try:
                        record = json.loads(line_str)
                        mac = record.get("mac")
                        ts = record.get("timestamp", time.time())
                        rid_info = record.get("remote_id")

                        if not mac or mac == "UNKNOWN" or not rid_info:
                            continue

                        parsed_msgs = rid_info.get("parsed_messages", [])
                        raw_hex = record.get("raw_hex", "")
                        transport_type = str(rid_info.get("transport", "")).lower()
                        pdu_type = str(record.get("pdu_type", "")).upper()
                        active_mode = str(record.get("active_ble_mode", "")).lower()

                        if (
                            "5" in transport_type
                            or "ext" in transport_type
                            or "AUX" in pdu_type
                            or "EXT" in pdu_type
                        ):
                            transport = "bt5"
                        elif "4" in transport_type or "legacy" in transport_type or active_mode == "bt4":
                            transport = "bt4"
                        elif active_mode == "bt5":
                            transport = "bt5"
                        else:
                            transport = "bt5" if self.ble_mode in ("ble5", "extended") else "bt4"

                        # If parsed_msgs is empty or missing fields, try parsing raw_hex if available
                        if not parsed_msgs and raw_hex:
                            try:
                                raw_bytes = bytes.fromhex(raw_hex)
                                uuid_idx = raw_bytes.find(BLE_RID_UUID)
                                if uuid_idx != -1:
                                    astm_p = raw_bytes[uuid_idx + 2:]
                                    if len(astm_p) >= 2 and astm_p[0] == 0x0D:
                                        astm_p = astm_p[2:]
                                    parsed_msgs, _ = parse_astm_payload(astm_p)
                            except Exception:
                                pass

                        # Generate messages_b64 from parsed message hex blocks or raw
                        messages_b64 = []
                        for msg in parsed_msgs:
                            if isinstance(msg, dict) and "raw_hex" in msg:
                                raw_b = bytes.fromhex(msg["raw_hex"])
                                messages_b64.append(base64.b64encode(raw_b[:25]).decode('ascii'))

                        serial_no = None
                        for msg in parsed_msgs:
                            if isinstance(msg, dict) and msg.get("type") == "Basic ID" and msg.get("id"):
                                serial_no = msg["id"]
                                break

                        counter = rid_info.get("counter", 0)

                        rf_ch = record.get("rf_channel")
                        ch_str = f"Ch {rf_ch}" if rf_ch is not None else "Adv (37/38/39)"
                        freq_mhz = 2402
                        if rf_ch == 37:
                            freq_mhz = 2402
                        elif rf_ch == 38:
                            freq_mhz = 2426
                        elif rf_ch == 39:
                            freq_mhz = 2480
                        elif rf_ch is not None and 0 <= rf_ch <= 36:
                            freq_mhz = 2404 + 2 * rf_ch

                        event: Dict[str, Any] = {
                            "timestamp": ts,
                            "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                            "transport": transport,
                            "interface": "nRF52840-UART",
                            "channel": ch_str,
                            "band": "2.4GHz",
                            "frequency_mhz": freq_mhz,
                            "rate_mbps": 1.0,
                            "modulation": "GFSK",
                            "rate_desc": "1.0 Mbps (LE 1M GFSK)",
                            "bandwidth_mhz": 2,
                            "mcs_index": None,
                            "guard_interval": None,
                            "counter": counter,
                            "mac": mac.upper(),
                            "rssi_dbm": record.get("rssi_dbm"),
                            "serial_number": serial_no,
                            "messages": parsed_msgs,
                            "messages_b64": messages_b64,
                            "raw_length": record.get("raw_length", 0),
                            "pdu_type": record.get("pdu_type"),
                        }

                        self.event_queue.put(event)

                    except Exception as e:
                        logger.debug(f"Error parsing BLE JSON record: {e}")

            except Exception as e:
                if self.running:
                    logger.error(f"[-] Error running nrf_bt_sniffer_json.py: {e}")
            finally:
                self.stop_process()

            # If offline PCAP mode, stop after completion
            if self.rx_pcap or not self.running:
                break

            # If live sniffing and process ended unexpectedly, wait and retry
            logger.warning("[!] BLE nRF sniffer disconnected or exited. Reconnecting in 2.0s...")
            time.sleep(2.0)

        self.running = False
        logger.info("[*] BLE nRF Sniffer worker stopped.")

    def stop_process(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                time.sleep(0.1)
                if self.proc.poll() is None:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def stop(self):
        self.running = False
        self.stop_process()


# ============================================================================
# Main / Real-Time Formatted Console & Storage Logger
# ============================================================================

class UnifiedTelemetryLogger:
    """
    Consumes parsed RID events and manages:
    1. Colorized live console display (or periodic quiet heartbeat in daemon mode).
    2. Append-only replay-compatible JSONL log with optional daily date splitting.
    3. SQLite 5-minute flight encounter grouping & throttled persistence with WAL checkpointing.
    4. Distributed streaming via CentralStreamForwarder (3-tier reliability).
    """
    def __init__(
        self,
        log_jsonl_path: Optional[str] = None,
        db_path: Optional[str] = "rid_detections.db",
        encounter_timeout_s: float = 300.0,
        quiet: bool = False,
        rotate_daily: bool = False,
        persist_interval_s: float = 2.0,
        forwarder: Optional[Any] = None,
        node_id: Optional[str] = None,
        node_meta: Optional[Dict[str, Any]] = None,
    ):
        self.forwarder = forwarder
        self.node_id = node_id
        self.node_meta = node_meta or {}
        self.base_log_path = log_jsonl_path
        self.rotate_daily = rotate_daily
        self.quiet = quiet
        self.current_log_path: Optional[str] = None
        self.log_file_handle = None

        self.encounter_tracker = EncounterTracker(
            db_path=db_path,
            timeout_s=encounter_timeout_s,
            persist_interval_s=persist_interval_s,
            default_node_id=self.node_id,
        ) if db_path else None

        # Register local node in database if local SQLite DB active
        if db_path and self.node_id:
            try:
                with sqlite3.connect(db_path) as conn:
                    upsert_receiver_node(
                        conn,
                        node_id=self.node_id,
                        name=self.node_meta.get("name", self.node_id),
                        latitude=float(self.node_meta.get("latitude", 0.0)),
                        longitude=float(self.node_meta.get("longitude", 0.0)),
                        altitude_m=float(self.node_meta.get("altitude_m", 0.0)),
                        range_rings_json=json.dumps(self.node_meta.get("range_rings_m", [500, 1000, 2500, 5000])),
                        description=self.node_meta.get("description", ""),
                        locked=bool(self.node_meta.get("locked", False)),
                        status="ONLINE",
                    )
            except Exception as e:
                logger.debug(f"Could not register local receiver node in DB: {e}")

        self.stats = {
            "total_packets": 0,
            "transports": {"bt4": 0, "bt5": 0, "wifi": 0, "nan": 0},
            "macs": set(),
            "serials": set(),
            "wifi_channels": {},
            "ble_channels": {},
        }
        self.mac_to_serial: Dict[str, str] = {}
        self.start_time = time.time()
        self.first_packet_time: Optional[float] = None
        self.last_timeout_check = time.time()
        self.last_heartbeat = time.time()
        self.last_node_heartbeat = time.time()

    def _ensure_log_handle(self, now: float):
        if not self.base_log_path:
            return None

        if self.rotate_daily:
            dt_str = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
            root, ext = os.path.splitext(self.base_log_path)
            target = f"{root}_{dt_str}{ext or '.jsonl'}"
        else:
            target = self.base_log_path

        if target != self.current_log_path or self.log_file_handle is None:
            if self.log_file_handle:
                try:
                    self.log_file_handle.close()
                except Exception:
                    pass
            parent_dir = os.path.dirname(os.path.abspath(target))
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            self.current_log_path = target
            self.log_file_handle = open(target, "a")
            if not self.quiet:
                logger.info(f"[*] Replay telemetry logging to {target}")

        return self.log_file_handle

    def periodic_maintenance(self, now: Optional[float] = None):
        """
        Periodic maintenance task called continuously from the main loop even when the event queue is empty.
        1. Sweeps for encounters exceeding the silence timeout (> 5 minutes) and closes them in SQLite.
        2. In quiet / daemon mode, prints a periodic status heartbeat every 30 seconds.
        3. Refreshes local node heartbeat in SQLite so local dashboards show node ONLINE.
        """
        if now is None:
            now = time.time()

        # 1. Sweep for timed-out encounters every 5 seconds
        if self.encounter_tracker and (now - self.last_timeout_check >= 5.0):
            closed = self.encounter_tracker.check_timeouts(now)
            for c_id in closed:
                if not self.quiet:
                    print(f"{C_GRAY}[*] Flight Encounter {c_id} closed ({self.encounter_tracker.timeout_s:.0f}s silence timeout).{C_RESET}")
            self.last_timeout_check = now

        # 2. Touch local receiver node heartbeat in SQLite every 15s
        if self.encounter_tracker and self.encounter_tracker.db_path and self.node_id:
            if now - self.last_node_heartbeat >= 15.0:
                self.last_node_heartbeat = now
                try:
                    with sqlite3.connect(self.encounter_tracker.db_path) as conn:
                        touch_receiver_node_heartbeat(conn, self.node_id)
                except Exception:
                    pass

        # 3. In quiet mode, emit periodic heartbeat every 30s even when 0 packets arrive
        if self.quiet and (now - self.last_heartbeat >= 30.0):
            self.last_heartbeat = now
            iso_str = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            active_cnt = len(self.encounter_tracker.active_encounters) if self.encounter_tracker else 0
            bt5_cnt = self.stats["transports"].get("bt5", 0)
            bt4_cnt = self.stats["transports"].get("bt4", 0)
            bt_cnt = bt5_cnt + bt4_cnt
            wifi_cnt = self.stats["transports"].get("wifi", 0) + self.stats["transports"].get("nan", 0)
            hub_str = ""
            if self.forwarder:
                h_status = "CONNECTED" if self.forwarder.connected else "CONNECTING/RETRYING"
                streamed = self.forwarder.stats.get("packets_streamed_live", 0)
                hub_str = f" | Hub: {h_status} (Streamed: {streamed})"
            else:
                hub_str = " | Hub: Disabled (Standalone Mode)"
            print(f"[STATUS {iso_str}] Total: {self.stats['total_packets']} pkts (BLE: {bt_cnt} [BT5: {bt5_cnt}, BT4: {bt4_cnt}], Wi-Fi: {wifi_cnt}) | Active Encounters: {active_cnt} | Unique Drones: {len(self.stats['macs'])}{hub_str}")
            sys.stdout.flush()

    def process_event(self, event: Dict[str, Any]):
        now = time.time()
        if self.first_packet_time is None:
            self.first_packet_time = event.get("timestamp", now)

        # Calculate time_offset_ms relative to first packet for replay_drones.py
        time_offset_ms = int((event.get("timestamp", now) - self.first_packet_time) * 1000)

        self.stats["total_packets"] += 1
        t_key = event.get("transport", "unknown")
        self.stats["transports"][t_key] = self.stats["transports"].get(t_key, 0) + 1

        mac = event.get("mac", "UNKNOWN")
        self.stats["macs"].add(mac)

        serial = event.get("serial_number")
        if serial:
            self.stats["serials"].add(serial)
            self.mac_to_serial[mac] = serial
        elif mac in self.mac_to_serial:
            serial = self.mac_to_serial[mac]
            event["serial_number"] = serial

        ch_raw = event.get("channel")
        if t_key in ("bt4", "bt5"):
            ch_key = f"Ch {ch_raw}" if (isinstance(ch_raw, int) or (isinstance(ch_raw, str) and ch_raw.isdigit())) else str(ch_raw or "Adv")
            self.stats["ble_channels"][ch_key] = self.stats["ble_channels"].get(ch_key, 0) + 1
        elif t_key in ("wifi", "nan"):
            ch_key = str(ch_raw) if ch_raw is not None else "N/A"
            self.stats["wifi_channels"][ch_key] = self.stats["wifi_channels"].get(ch_key, 0) + 1

        # 1. Forward to Central Hub (if in distributed streaming mode)
        if self.forwarder:
            self.forwarder.enqueue_packet(event)

        # 2. Update SQLite 5-minute Encounter Tracker (if configured)
        encounter_id = None
        if self.encounter_tracker:
            encounter_id = self.encounter_tracker.update_with_packet(event)

        # 3. Write to Replay-Compatible JSONL (if configured)
        ev_ts = event.get("timestamp", now)
        handle = self._ensure_log_handle(ev_ts)
        if handle:
            replay_record = {
                "timestamp": ev_ts,
                "timestamp_iso": event.get("timestamp_iso"),
                "time_offset_ms": time_offset_ms,
                "transport": t_key,
                "counter": event.get("counter", 0),
                "messages_b64": event.get("messages_b64", []),
                "mac": mac,
                "serial": serial,
                "channel": event.get("channel"),
                "rssi_dbm": event.get("rssi_dbm"),
                "rate_mbps": event.get("rate_mbps"),
                "modulation": event.get("modulation"),
                "rate_desc": event.get("rate_desc"),
                "bandwidth_mhz": event.get("bandwidth_mhz"),
                "mcs_index": event.get("mcs_index"),
                "guard_interval": event.get("guard_interval"),
                "encounter_id": encounter_id,
            }
            handle.write(json.dumps(replay_record) + "\n")
            handle.flush()

        # In quiet mode, per-packet display is suppressed (heartbeats handled in periodic_maintenance)
        if self.quiet:
            return

        # 3. Format Live Console Banner
        transport_badges = {
            "bt4": f"{C_BLUE}[BLE 4 LEGACY]{C_RESET}",
            "bt5": f"{C_CYAN}[BLE 5 EXT]{C_RESET}",
            "wifi": f"{C_GREEN}[WIFI BEACON]{C_RESET}",
            "nan": f"{C_YELLOW}[WIFI NAN]{C_RESET}",
        }
        badge = transport_badges.get(t_key, f"{C_WHITE}[{t_key.upper()}]{C_RESET}")

        rssi_val = event.get("rssi_dbm")
        rssi_str = f"{rssi_val:+d} dBm" if rssi_val is not None else "Unknown RSSI"

        band_str = event.get("band", "")
        if t_key in ("bt4", "bt5"):
            ch_display = f"BLE {ch_raw}" if ch_raw and not str(ch_raw).startswith("BLE") else str(ch_raw or "Adv")
        else:
            ch_display = f"Ch {ch_raw}" if ch_raw and not str(ch_raw).startswith("Ch") else str(ch_raw or "")
        rate_tag = f" [{event['rate_desc']}]" if event.get("rate_desc") else ""
        rf_info = f"{band_str} {ch_display}{rate_tag}".strip()

        dt_str = datetime.fromtimestamp(event.get("timestamp", now)).strftime("%H:%M:%S.%f")[:-3]
        enc_tag = f" {C_GRAY}({encounter_id}){C_RESET}" if encounter_id else ""

        print(f"\n{C_BOLD}🚁 DRONE RID DETECTED {badge}{enc_tag} {C_GRAY}{dt_str}{C_RESET}")
        print(f"   {C_WHITE}MAC: {C_BOLD}{mac}{C_RESET} | {C_WHITE}RSSI: {C_BOLD}{rssi_str}{C_RESET} | {C_WHITE}RF: {C_MAGENTA}{rf_info}{C_RESET}")
        if serial:
            inf = infer_drone_model(serial)
            model_tag = f" {C_YELLOW}[{inf['make']} {inf['model']}]{C_RESET}" if inf.get("is_inferred") else ""
            print(f"   {C_GREEN}Serial / UAS ID: {C_BOLD}{serial}{C_RESET}{model_tag}")

        # Print decoded telemetry blocks
        for msg in event.get("messages", []):
            m_type = msg.get("type", "Unknown")
            proto_name = msg.get("proto_version_name", "")
            ver_tag = f" {C_GRAY}[{proto_name}]{C_RESET}" if proto_name else ""

            if m_type == "Location":
                lat = msg.get("lat")
                lon = msg.get("lon")
                alt = msg.get("geodetic_altitude_m") or msg.get("pressure_altitude_m")
                height = msg.get("height_m")
                h_type_name = msg.get("height_type_name", "")
                spd = msg.get("speed_mps")
                heading = msg.get("direction_deg")
                status_name = msg.get("status_name", "")

                loc_parts = []
                if status_name:
                    loc_parts.append(f"Status: {status_name}")
                if lat is not None and lon is not None:
                    loc_parts.append(f"Pos: ({lat:.6f}, {lon:.6f})")
                if alt is not None:
                    loc_parts.append(f"Alt: {alt:.1f}m")
                if height is not None:
                    loc_parts.append(f"H: {height:.1f}m ({h_type_name})")
                if spd is not None:
                    loc_parts.append(f"Speed: {spd:.1f}m/s")
                if heading is not None:
                    loc_parts.append(f"Hdg: {heading}°")

                acc_parts = []
                if msg.get("horizontal_accuracy_name") and msg.get("horizontal_accuracy", 0) > 0:
                    acc_parts.append(f"HAcc: {msg['horizontal_accuracy_name']}")
                if msg.get("vertical_accuracy_name") and msg.get("vertical_accuracy", 0) > 0:
                    acc_parts.append(f"VAcc: {msg['vertical_accuracy_name']}")
                if msg.get("speed_accuracy_name") and msg.get("speed_accuracy", 0) > 0:
                    acc_parts.append(f"SpdAcc: {msg['speed_accuracy_name']}")

                print(f"   {C_CYAN}📍 Location  {C_RESET}{ver_tag} -> {' | '.join(loc_parts)}")
                if acc_parts:
                    print(f"      {C_GRAY}Accuracies: {', '.join(acc_parts)}{C_RESET}")

            elif m_type == "Basic ID":
                b_id = msg.get("id")
                id_type_name = msg.get("id_type_name", f"Type {msg.get('id_type')}")
                ua_type_name = msg.get("ua_type_name", f"UA {msg.get('ua_type')}")
                print(f"   {C_YELLOW}🆔 Basic ID  {C_RESET}{ver_tag} -> ID: {b_id} | Type: {id_type_name} | Aircraft: {ua_type_name}")

            elif m_type == "System":
                p_lat = msg.get("pilot_lat")
                p_lon = msg.get("pilot_lon")
                p_alt = msg.get("pilot_alt_m")
                radius = msg.get("area_radius_m")
                op_loc_type = msg.get("operator_location_type_name", "")
                class_type = msg.get("classification_type_name", "")

                sys_parts = []
                if p_lat is not None and p_lon is not None:
                    sys_parts.append(f"Pilot: ({p_lat:.6f}, {p_lon:.6f}) [{op_loc_type}]")
                if p_alt is not None:
                    sys_parts.append(f"Alt: {p_alt:.1f}m")
                if radius:
                    sys_parts.append(f"Radius: {radius}m")
                if msg.get("classification_type") == 1:  # EU
                    sys_parts.append(f"EU Category: {msg.get('category_eu_name')} / {msg.get('class_eu_name')}")
                if msg.get("system_timestamp_iso"):
                    sys_parts.append(f"TS: {msg['system_timestamp_iso']}")

                print(f"   {C_MAGENTA}🎮 System    {C_RESET}{ver_tag} -> {' | '.join(sys_parts)}")

            elif m_type == "Operator ID":
                op_id = msg.get("operator_id") or msg.get("id")
                op_id_display = op_id if op_id else "None / Unset"
                op_id_type = msg.get("operator_id_type_name", "Operator ID")
                print(f"   {C_WHITE}👤 Operator  {C_RESET}{ver_tag} -> {op_id_type}: {op_id_display}")

            elif m_type == "Self-ID":
                desc = msg.get("description") or msg.get("desc")
                desc_display = desc if desc else "None / Unset"
                desc_type = msg.get("desc_type_name", "Text Description")
                print(f"   {C_WHITE}📝 Self-ID   {C_RESET}{ver_tag} -> [{desc_type}] \"{desc_display}\"")

            elif m_type == "Auth":
                auth_type_name = msg.get("auth_type_name", "Auth")
                page_num = msg.get("page_number", 0)
                auth_hex = msg.get("auth_data_hex", "")
                if page_num == 0:
                    auth_len = msg.get("auth_data_length", len(auth_hex)//2)
                    print(f"   {C_BLUE}🔒 Auth      {C_RESET}{ver_tag} -> Type: {auth_type_name} | Page: 0/{msg.get('last_page_index', 0)} (Len: {auth_len}B) | Hex: {auth_hex[:32]}...")
                else:
                    print(f"   {C_BLUE}🔒 Auth      {C_RESET}{ver_tag} -> Type: {auth_type_name} | Page: {page_num} | Hex: {auth_hex[:32]}...")

        sys.stdout.flush()

    def close(self):
        if self.forwarder:
            self.forwarder.stop()
        if self.encounter_tracker:
            self.encounter_tracker.finalize_all()
        if self.log_file_handle:
            try:
                self.log_file_handle.close()
            except Exception:
                pass
            self.log_file_handle = None

    def print_summary(self):
        duration = time.time() - self.start_time
        print(f"\n{C_BOLD}{'='*60}{C_RESET}")
        print(f"{C_BOLD}📊 CAPTURE SUMMARY ({duration:.1f}s elapsed){C_RESET}")
        print(f"{C_BOLD}{'='*60}{C_RESET}")
        print(f"  • Total RID Packets Captured : {C_BOLD}{self.stats['total_packets']}{C_RESET}")
        print(f"  • Unique Drones (MACs)       : {C_BOLD}{len(self.stats['macs'])}{C_RESET}")
        print(f"  • Unique UAS Serial Numbers  : {C_BOLD}{len(self.stats['serials'])}{C_RESET}")
        print(f"  • Physical Transport Breakdown:")
        for t_name, count in self.stats["transports"].items():
            print(f"      - {t_name:<12}: {count}")

        if self.stats["wifi_channels"]:
            print(f"  • Wi-Fi Channels Active (2.4GHz / 5.8GHz):")
            def _wifi_sort_key(c):
                try:
                    return (0, int(c))
                except ValueError:
                    return (1, str(c))
            for ch in sorted(self.stats["wifi_channels"].keys(), key=_wifi_sort_key):
                print(f"      - Channel {ch:<8}: {self.stats['wifi_channels'][ch]} packets")

        if self.stats["ble_channels"]:
            print(f"  • Bluetooth LE Channels Active:")
            for ch in sorted(self.stats["ble_channels"].keys()):
                print(f"      - {ch:<16}: {self.stats['ble_channels'][ch]} packets")

        print(f"{C_BOLD}{'='*60}{C_RESET}\n")


# ============================================================================
# Monitor Mode Setup Helpers
# ============================================================================

def setup_monitor_mode(interface: str, initial_channel: int = 6):
    """Put interface into monitor mode, unmanage from NetworkManager, and bring it up."""
    logger.info(f"[*] Configuring {interface} into monitor mode...")
    try:
        # Attempt to unmanage from NetworkManager to prevent channel hopping interference
        try:
            subprocess.run(["nmcli", "dev", "set", interface, "managed", "no"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass

        subprocess.run(["ip", "link", "set", interface, "down"], check=True)
        subprocess.run(["iw", "dev", interface, "set", "type", "monitor"], check=True)
        subprocess.run(["ip", "link", "set", interface, "up"], check=True)
        subprocess.run(["iw", "dev", interface, "set", "channel", str(initial_channel)], check=True)
        logger.info(f"[*] {interface} is ready in monitor mode on Channel {initial_channel}.")
    except Exception as e:
        logger.warning(f"[!] Warning: Could not configure monitor mode on {interface}: {e}")


def restore_managed_mode(interface: str):
    """Restore interface to managed mode."""
    logger.info(f"[*] Restoring {interface} to managed mode...")
    try:
        subprocess.run(["ip", "link", "set", interface, "down"], check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["iw", "dev", interface, "set", "type", "managed"], check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["ip", "link", "set", interface, "up"], check=True, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.debug(f"Failed to restore {interface}: {e}")


# ============================================================================
# Main Entrypoint & CLI Parsing
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Combined Bluetooth (nRF UART) and Wi-Fi Drone Remote ID Listener and Console Logger",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Hardware & Interfaces
    parser.add_argument("--wifi-iface", "-i", default=None, help="Wi-Fi monitor-mode interface (e.g., wlan1)")
    parser.add_argument("--nrf-port", "-p", default=None, help="nRF Sniffer UART serial port (e.g., /dev/ttyACM0). Auto-detected if omitted.")
    parser.add_argument("--rx-pcap", help="Replay pre-captured BLE pcap file for offline verification")

    # Subsystem toggles
    parser.add_argument("--no-wifi", action="store_true", help="Disable Wi-Fi sniffing and channel hopping")
    parser.add_argument("--no-ble", action="store_true", help="Disable Bluetooth sniffing")
    parser.add_argument("--no-wifi-setup", action="store_true", help="Skip bringing Wi-Fi interface down/up into monitor mode")

    # Hopping Schedule Configuration
    parser.add_argument("--wifi-channel", "--channel", "-c", type=int, default=None,
                        help="Lock Wi-Fi sniffer to a single fixed channel (e.g. 6 or 149) and disable hopping")
    parser.add_argument("--no-hop", action="store_true",
                        help="Disable Wi-Fi channel hopping (listen only on initial channel)")
    parser.add_argument("--non-social-ratio", "-k", type=int, default=1,
                        help="Non-social channel ratio multiplier k (cycles 2k non-social on 2.4GHz for every k on 5.8GHz)")
    parser.add_argument("--social-dwell-ms", type=int, default=1000, help="Social channel dwell time in milliseconds (1 Hz)")
    parser.add_argument("--non-social-dwell-ms", type=int, default=200, help="2.4 GHz non-social channel dwell time in milliseconds (default: 200ms)")
    parser.add_argument("--non-social-dwell-5g-ms", type=int, default=250, help="5.8 GHz non-social channel dwell time in milliseconds (default: 250ms extended dwell to compensate for mixed/negative PLL lead times)")
    parser.add_argument("--drain-retention-ms", type=float, default=5.0, help="Buffer drain retention window in ms for previous channel packet attribution (default: 5.0ms)")

    # Distributed Hub & Forwarding Options
    try:
        cfg = load_scanner_config() if load_scanner_config else {}
    except Exception:
        cfg = {}
    default_node_id = cfg.get("node_id", "sensor-node-01")
    default_hub_url = cfg.get("hub_ws_url")
    default_spool_dir = cfg.get("spool_dir", "spool")
    default_max_ram = int(cfg.get("max_ram_queue", 10000))
    default_ble_mode = cfg.get("ble_mode", "hop")
    default_ble_bt5_dwell = float(cfg.get("ble_bt5_dwell_s", 5.0))
    default_ble_bt4_dwell = float(cfg.get("ble_bt4_dwell_s", 1.0))

    # BLE Options
    parser.add_argument("--coded", action="store_true", help="Enable Bluetooth 5 Long Range (LE Coded PHY) scanning")
    parser.add_argument("--ble-mode", choices=["hop", "extended", "legacy", "all"], default=default_ble_mode, help="BLE advertisement filter mode (default: hop)")
    parser.add_argument("--ble-bt5-dwell", type=float, default=default_ble_bt5_dwell, help="BT5 Extended / Coded dwell time in seconds (default: 5.0s)")
    parser.add_argument("--ble-bt4-dwell", type=float, default=default_ble_bt4_dwell, help="BT4 Legacy dwell time in seconds (default: 1.0s)")

    parser.add_argument("--scanner-config", default=None, help="Path to JSON configuration file for scanner station parameters (default: scanner/scanner_config.json)")
    parser.add_argument("--hub-url", default=default_hub_url, help="Central Ingestion Hub WebSocket URL (e.g. ws://hub-ip:8000/stream/node)")
    parser.add_argument("--node-id", default=default_node_id, help="Sensor node identifier")
    parser.add_argument("--standalone", action="store_true", help="Force standalone mode (disables forwarding, enables local SQLite/JSONL)")
    parser.add_argument("--spool-dir", default=default_spool_dir, help="Directory for temporary spool files during network outages")
    parser.add_argument("--max-ram-queue", type=int, default=default_max_ram, help="Max in-memory RAM queue size before spilling to disk")

    # Storage & Logging
    parser.add_argument("--db-file", default="rid_detections.db", help="SQLite database path for 5-minute flight encounter records (set empty '' to disable)")
    parser.add_argument("--encounter-timeout-s", type=float, default=300.0, help="Flight encounter timeout in seconds (default 300s / 5 minutes)")
    parser.add_argument("--persist-interval", type=float, default=2.0, help="Maximum frequency in seconds to persist active encounters to SQLite (default: 2.0s)")
    parser.add_argument("--log-jsonl", default=None, help="Optional replay-compatible JSONL log file path")
    parser.add_argument("--rotate-daily", action="store_true", help="Automatically split JSONL log file daily (<name>_YYYYMMDD.jsonl)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet / daemon mode: suppress per-packet console banner and print periodic heartbeat status")
    parser.add_argument("--sync-only", action="store_true", help="Synchronize pending spool files and historical backlog to Central Hub and exit")
    parser.add_argument("--rehydrate", action="store_true", help="Retroactively re-parse all raw base64 ASTM messages from rid_packets_*.jsonl files and update the SQLite database")

    args = parser.parse_args()

    if args.scanner_config:
        try:
            cfg = load_scanner_config(args.scanner_config)
        except Exception as e:
            logger.warning(f"[!] Warning: Failed to parse scanner config '{args.scanner_config}': {e}")
            logger.warning("[!] Scanner is continuing in Standalone Local Mode with default parameters.")
            cfg = {}
        if args.hub_url == default_hub_url:
            args.hub_url = cfg.get("hub_ws_url")
        if args.node_id == default_node_id:
            args.node_id = cfg.get("node_id", default_node_id)
        if args.spool_dir == default_spool_dir:
            args.spool_dir = cfg.get("spool_dir", default_spool_dir)
        if args.max_ram_queue == default_max_ram:
            args.max_ram_queue = int(cfg.get("max_ram_queue", default_max_ram))
        if args.ble_mode == default_ble_mode:
            args.ble_mode = cfg.get("ble_mode", default_ble_mode)
        if args.ble_bt5_dwell == default_ble_bt5_dwell:
            args.ble_bt5_dwell = float(cfg.get("ble_bt5_dwell_s", default_ble_bt5_dwell))
        if args.ble_bt4_dwell == default_ble_bt4_dwell:
            args.ble_bt4_dwell = float(cfg.get("ble_bt4_dwell_s", default_ble_bt4_dwell))

    if not args.no_wifi and not args.wifi_iface:
        args.no_wifi = True

    if os.geteuid() != 0 and not args.no_wifi:
        logger.warning("[!] Warning: Root privileges (sudo) are recommended for Wi-Fi monitor mode and raw socket capture.")

    initial_wifi_ch = args.wifi_channel if args.wifi_channel is not None else SOCIAL_CHANNEL_2G

    if args.rehydrate:
        rehydrated = rehydrate_db_from_jsonl(
            db_path=args.db_file if args.db_file else "rid_detections.db",
            node_id=args.node_id,
        )
        print(f"{C_GREEN}[+] Database rehydration complete: {rehydrated} encounter(s) updated in {args.db_file or 'rid_detections.db'}.{C_RESET}")
        if not args.hub_url and not args.wifi_iface and not args.nrf_port:
            return

    # Configure Distributed Stream Forwarder or Standalone Mode
    is_hub_mode = bool(args.hub_url and not args.standalone)
    forwarder = None

    if is_hub_mode:
        if CentralStreamForwarder is not None:
            # Auto-detect historical backlog logs in working dir & repo dir (rid_packets_*.jsonl, rid_packets.jsonl)
            backlog_patterns = [
                os.path.join(os.getcwd(), "rid_packets*.jsonl"),
                os.path.join(os.getcwd(), "capture*.jsonl"),
            ]
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            if os.path.abspath(os.getcwd()) != repo_root:
                backlog_patterns.append(os.path.join(repo_root, "rid_packets*.jsonl"))
                backlog_patterns.append(os.path.join(repo_root, "capture*.jsonl"))

            forwarder = CentralStreamForwarder(
                hub_ws_url=args.hub_url,
                node_id=args.node_id,
                node_meta=cfg,
                spool_dir=args.spool_dir,
                backlog_paths=backlog_patterns,
                max_ram_queue=args.max_ram_queue,
                quiet=args.quiet,
            )
            forwarder.start()
            logger.info(f"[*] Operational Mode: CENTRALIZED STREAMING -> Hub: {args.hub_url} | Node ID: {args.node_id}")

            if args.sync_only:
                logger.info("[*] --sync-only specified: Waiting for catch-up backlog upload to complete...")
                forwarder.catchup_complete.wait(timeout=120.0)
                time.sleep(1.0)
                forwarder.stop()
                logger.info("[+] Catch-up backlog synchronization complete. Exiting.")
                return
        else:
            logger.error("[-] CentralStreamForwarder module could not be loaded. Running standalone.")
    else:
        logger.info(f"[*] Operational Mode: STANDALONE LOCAL -> Encounters DB: {args.db_file or 'disabled'} (No Hub URL configured)")

    if args.no_wifi and args.no_ble:
        if is_hub_mode and forwarder:
            logger.info("[*] Radios disabled (--no-wifi --no-ble). Stream forwarder is running in background to drain spool/backlog.")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                logger.info("[*] Stopping forwarder...")
                forwarder.stop()
                return
        else:
            logger.error("[-] Both Wi-Fi and BLE are disabled and no Hub URL configured. Nothing to do!")
            sys.exit(1)

    # Determine local storage paths: in hub mode, disable continuous local disk writes unless explicitly specified
    db_path_to_use = None
    if not is_hub_mode:
        db_path_to_use = args.db_file if args.db_file else None
    elif args.db_file and args.db_file != "rid_detections.db":
        db_path_to_use = args.db_file

    log_jsonl_to_use = args.log_jsonl if (not is_hub_mode or args.log_jsonl) else None

    event_queue: queue.Queue = queue.Queue()
    channel_state = SharedChannelState(initial_channel=initial_wifi_ch, drain_retention_ms=args.drain_retention_ms)
    logger_worker = UnifiedTelemetryLogger(
        log_jsonl_path=log_jsonl_to_use,
        db_path=db_path_to_use,
        encounter_timeout_s=args.encounter_timeout_s,
        quiet=args.quiet,
        rotate_daily=args.rotate_daily,
        persist_interval_s=args.persist_interval,
        forwarder=forwarder,
        node_id=args.node_id,
        node_meta=cfg,
    )

    threads: List[threading.Thread] = []
    hopper_thread: Optional[WifiChannelHopperThread] = None
    wifi_thread: Optional[WifiSnifferThread] = None
    ble_thread: Optional[BleNrfSnifferThread] = None

    # Setup Wi-Fi
    if not args.no_wifi:
        if not args.no_wifi_setup:
            setup_monitor_mode(args.wifi_iface, initial_channel=initial_wifi_ch)

        if not args.no_hop and args.wifi_channel is None:
            hopper_thread = WifiChannelHopperThread(
                interface=args.wifi_iface,
                channel_state=channel_state,
                non_social_ratio_k=args.non_social_ratio,
                social_dwell_ms=args.social_dwell_ms,
                non_social_dwell_ms=args.non_social_dwell_ms,
                non_social_dwell_5g_ms=args.non_social_dwell_5g_ms,
            )
            threads.append(hopper_thread)
        else:
            logger.info(f"[*] Wi-Fi sniffer locked to fixed Channel {initial_wifi_ch} (hopping disabled).")

        wifi_thread = WifiSnifferThread(
            interface=args.wifi_iface,
            channel_state=channel_state,
            event_queue=event_queue,
        )
        threads.append(wifi_thread)

    # Setup BLE
    if not args.no_ble:
        nrf_port = args.nrf_port
        if not nrf_port and not args.rx_pcap:
            for candidate in ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"]:
                if os.path.exists(candidate):
                    nrf_port = candidate
                    logger.info(f"[*] Auto-detected nRF BLE sniffer on {candidate}")
                    break
        ble_thread = BleNrfSnifferThread(
            event_queue=event_queue,
            nrf_port=nrf_port,
            rx_pcap=args.rx_pcap,
            coded=args.coded,
            ble_mode=args.ble_mode,
            bt5_dwell_s=args.ble_bt5_dwell,
            bt4_dwell_s=args.ble_bt4_dwell,
        )
        threads.append(ble_thread)

    # Signal Handling for graceful shutdown
    stop_event = threading.Event()

    def shutdown(signum, frame):
        if not stop_event.is_set():
            stop_event.set()
            print(f"\n{C_YELLOW}[*] Shutting down combined listener...{C_RESET}")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"\n{C_BOLD}{C_GREEN}🚀 COMBINED BLUETOOTH & WI-FI REMOTE ID LISTENER ACTIVE{C_RESET}")
    if is_hub_mode and forwarder:
        print(f"  • Central Hub URL    : {C_CYAN}{args.hub_url}{C_RESET} (Node ID: {C_BOLD}{args.node_id}{C_RESET})")
        print(f"  • Reliability Buffer : {C_MAGENTA}RAM Max: {args.max_ram_queue} pkts | Disk Spool: {args.spool_dir}/{C_RESET} (0 disk writes on happy path)")
    else:
        if db_path_to_use:
            print(f"  • SQLite Encounters DB: {C_MAGENTA}{db_path_to_use}{C_RESET} (Timeout: {args.encounter_timeout_s:.0f}s / {args.encounter_timeout_s/60:.1f}m)")
        if log_jsonl_to_use:
            print(f"  • Replay JSONL Log   : {C_MAGENTA}{log_jsonl_to_use}{C_RESET}")
    print(f"{C_GRAY}Press Ctrl+C at any time to stop and view capture statistics.{C_RESET}\n")

    # Start capture threads
    for t in threads:
        t.start()

    # Main logging loop
    try:
        while not stop_event.is_set():
            now = time.time()
            try:
                event = event_queue.get(timeout=0.2)
                logger_worker.process_event(event)
            except queue.Empty:
                pass
            logger_worker.periodic_maintenance(now)
    except KeyboardInterrupt:
        shutdown(None, None)
    finally:
        # Stop all worker threads
        if hopper_thread:
            hopper_thread.stop()
        if wifi_thread:
            wifi_thread.stop()
        if ble_thread:
            ble_thread.stop()

        for t in threads:
            t.join(timeout=1.0)

        # Restore Wi-Fi interface if we configured it
        if not args.no_wifi and not args.no_wifi_setup and args.wifi_iface:
            restore_managed_mode(args.wifi_iface)

        # Drain any remaining events in queue
        while not event_queue.empty():
            try:
                logger_worker.process_event(event_queue.get_nowait())
            except Exception:
                break

        # Finalize encounters & close database/logs
        logger_worker.close()
        logger_worker.print_summary()


if __name__ == "__main__":
    main()
