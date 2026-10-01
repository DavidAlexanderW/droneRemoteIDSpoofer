#!/usr/bin/env python3
"""
SQLite Encounter Database Manager
Groups individual Remote ID packets into 5-minute (300s) Flight Encounters
and commits completed/updated encounters into an SQLite database.
Merges sequential packets by both Serial number and MAC address.
Maintains running aggregates for RF and flight telemetry to optimize memory.
"""

import asyncio
import base64
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from scanner.parser import decode_astm_message, parse_astm_payload

from scanner.db import (
    haversine_m,
    init_encounters_db,
    is_better_operator_id,
    is_better_serial,
    is_fuzzy_operator_match,
    is_fuzzy_serial_match,
    sanitize_operator_id,
    sanitize_serial,
)
from scanner.drone_models import infer_drone_model
from scanner.timestamp_utils import resolve_reception_timestamp

logger = logging.getLogger("CombinedRIDListener.EncounterTracker")


class EncounterTracker:
    """
    Groups individual Remote ID packets into 5-minute (300s) Flight Encounters
    and commits completed/updated encounters into an SQLite database.
    Merges sequential packets by both Serial number and MAC address.
    """

    def __init__(
        self,
        db_path: Optional[str] = "rid_detections.db",
        timeout_s: float = 300.0,
        persist_interval_s: float = 0.0,
        default_node_id: Optional[str] = None,
    ):
        self.db_path = os.path.abspath(db_path) if db_path else None
        self.timeout_s = timeout_s
        self.persist_interval_s = persist_interval_s
        self.default_node_id = default_node_id
        self.active_encounters: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.last_wal_checkpoint = time.time()

        if self.db_path:
            db_dir = os.path.dirname(self.db_path)
            if db_dir and not os.path.exists(db_dir):
                try:
                    os.makedirs(db_dir, exist_ok=True)
                except Exception:
                    pass
            self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            init_encounters_db(conn, timeout_s=self.timeout_s)
        finally:
            conn.close()

    def _remove_active(self, enc: Dict[str, Any]):
        eid = enc.get("encounter_id")
        keys_to_del = [k for k, v in list(self.active_encounters.items()) if v.get("encounter_id") == eid]
        for k in keys_to_del:
            del self.active_encounters[k]

    def update_with_packet(self, packet: Dict[str, Any]) -> str:
        """Update or create an active encounter from an incoming packet. Returns encounter_id."""
        mac = packet.get("mac", "UNKNOWN")
        serial = packet.get("serial_number") if packet.get("serial_number") is not None else packet.get("serial")
        node_id = packet.get("node_id") or packet.get("primary_node_id") or self.default_node_id

        # Resolve messages upfront: decode messages_b64 if messages list is empty
        msgs_to_process = list(packet.get("messages") or [])
        if not msgs_to_process and packet.get("messages_b64"):
            for b64_str in packet["messages_b64"]:
                try:
                    raw_b = base64.b64decode(b64_str)
                    parsed, _ = parse_astm_payload(raw_b)
                    if parsed:
                        msgs_to_process.extend(parsed)
                    else:
                        dm = decode_astm_message(raw_b)
                        if dm:
                            msgs_to_process.append(dm)
                except Exception:
                    pass

        # Extract serial, operator ID, and location from messages upfront for encounter matching
        operator_id = packet.get("operator_id")
        pkt_lat = None
        pkt_lon = None
        for m in msgs_to_process:
            if not isinstance(m, dict):
                continue
            if m.get("is_known_version") is False or (m.get("protocol_version") is not None and m.get("protocol_version") not in (0, 1, 2)):
                continue
            m_type = m.get("type")
            if m_type == "Basic ID":
                b_id = m.get("id")
                id_t = m.get("id_type")
                if id_t == 1 or (id_t is None and not serial):
                    if not serial or is_better_serial(b_id, serial):
                        serial = b_id
                elif id_t == 2:
                    if not operator_id:
                        operator_id = b_id
            elif m_type == "Operator ID":
                op_val = m.get("operator_id") or m.get("id")
                if op_val and (not operator_id or is_better_operator_id(op_val, operator_id)):
                    operator_id = op_val
            elif m_type == "Location":
                if m.get("lat") is not None and m.get("lon") is not None:
                    pkt_lat = m.get("lat")
                    pkt_lon = m.get("lon")

        # Canonical physical reception timestamp resolution
        ts = resolve_reception_timestamp(packet)

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

        with self.lock:
            enc = None
            s_new = sanitize_serial(serial)
            op_new = sanitize_operator_id(operator_id)

            # 1. Exact or Fuzzy Serial Match
            if s_new:
                for active_enc in list(self.active_encounters.values()):
                    s_act = sanitize_serial(active_enc.get("serial_number"))
                    if s_act and (s_new == s_act or is_fuzzy_serial_match(s_new, s_act)):
                        enc = active_enc
                        break

            # 2. Exact or Fuzzy Operator ID Match
            if not enc and op_new:
                for active_enc in list(self.active_encounters.values()):
                    op_act = sanitize_operator_id(active_enc.get("operator_id"))
                    if op_act and (op_new == op_act or is_fuzzy_operator_match(op_new, op_act)):
                        enc = active_enc
                        break

            # 3. Exact MAC Match
            if not enc and mac and mac != "UNKNOWN" and mac in self.active_encounters:
                enc = self.active_encounters[mac]

            # 4. BLE Spatial Proximity Match (within 500m or realistic drone speed)
            if not enc and transport in ("bt4", "bt5") and pkt_lat is not None and pkt_lon is not None:
                for active_enc in list(self.active_encounters.values()):
                    if ("bt" in (active_enc.get("transports") or [])) and active_enc.get("trajectory"):
                        last_pt = active_enc["trajectory"][-1]
                        dist = haversine_m(pkt_lat, pkt_lon, last_pt[0], last_pt[1])
                        dt = abs(ts - active_enc["last_seen"])
                        if dt <= self.timeout_s and (dist <= 500.0 or dist <= max(1.0, dt) * 35.0):
                            s_act = sanitize_serial(active_enc.get("serial_number"))
                            if not s_new or not s_act or is_fuzzy_serial_match(s_new, s_act):
                                enc = active_enc
                                break

            if enc:
                is_timeout = (ts - enc["last_seen"] > self.timeout_s)
                # Only treat as major backward time jump if delta > 60s
                is_backward_jump = (enc["first_seen"] - ts > 60.0)
                if is_timeout or is_backward_jump:
                    # Finalize old encounter
                    enc["is_active"] = 0
                    self._persist_encounter(enc)
                    self._remove_active(enc)
                    enc = None

            if not enc:
                # Generate unique encounter ID or preserve existing
                enc_slug = mac.replace(":", "")[-6:] if mac and mac != "UNKNOWN" else "000000"
                dt_tag = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d-%H%M%S")
                encounter_id = packet.get("encounter_id") or f"ENC-{dt_tag}-{enc_slug}"

                best_initial_serial = s_new or serial
                drone_info = infer_drone_model(best_initial_serial) if best_initial_serial else {}
                enc = {
                    "encounter_id": encounter_id,
                    "mac": mac,
                    "serial_number": best_initial_serial,
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
                    "min_rssi": rssi if rssi is not None else None,
                    "max_rssi": rssi if rssi is not None else None,
                    "rssi_sum": float(rssi) if rssi is not None else 0.0,
                    "rssi_count": 1 if rssi is not None else 0,
                    "last_rssi": rssi,
                    "min_alt": None,
                    "max_alt": None,
                    "min_pressure_alt": None,
                    "max_pressure_alt": None,
                    "min_height": None,
                    "max_height": None,
                    "max_speed": None,
                    "max_vert_speed": None,
                    "pilot_lat": None,
                    "pilot_lon": None,
                    "pilot_alt_m": None,
                    "area_ceil_m": None,
                    "area_floor_m": None,
                    "operator_id": op_new or operator_id,
                    "self_id_desc": None,
                    "trajectory": [],
                    "is_active": 1,
                    "last_persisted": 0.0,
                }
                primary_key = mac if (mac and mac != "UNKNOWN") else encounter_id
                self.active_encounters[primary_key] = enc

            # If MAC is valid, ensure active_encounters indexes this MAC
            if mac and mac != "UNKNOWN":
                if mac in self.active_encounters and self.active_encounters[mac] is not enc:
                    # Merge previous active encounter tracking this MAC into enc
                    other_enc = self.active_encounters[mac]
                    enc["packet_count"] += other_enc.get("packet_count", 0)
                    enc["first_seen"] = min(enc["first_seen"], other_enc["first_seen"])
                    enc["last_seen"] = max(enc["last_seen"], other_enc["last_seen"])
                    enc["duration_s"] = round(enc["last_seen"] - enc["first_seen"], 2)
                    enc["transports"].update(other_enc.get("transports", set()))
                    enc["channels"].update(other_enc.get("channels", set()))
                    enc["wifi_rates"].update(other_enc.get("wifi_rates", set()))

                    # Merge PHY rate counts
                    if "rate_counts" in other_enc and other_enc["rate_counts"]:
                        if "rate_counts" not in enc:
                            enc["rate_counts"] = {}
                        for desc, info in other_enc["rate_counts"].items():
                            if desc not in enc["rate_counts"]:
                                enc["rate_counts"][desc] = dict(info)
                            else:
                                enc["rate_counts"][desc]["count"] += info.get("count", 0)

                    # Merge running aggregates
                    if other_enc.get("min_rssi") is not None:
                        enc["min_rssi"] = min(enc["min_rssi"], other_enc["min_rssi"]) if enc["min_rssi"] is not None else other_enc["min_rssi"]
                    if other_enc.get("max_rssi") is not None:
                        enc["max_rssi"] = max(enc["max_rssi"], other_enc["max_rssi"]) if enc["max_rssi"] is not None else other_enc["max_rssi"]
                    enc["rssi_sum"] += other_enc.get("rssi_sum", 0.0)
                    enc["rssi_count"] += other_enc.get("rssi_count", 0)
                    if other_enc.get("last_rssi") is not None:
                        enc["last_rssi"] = other_enc["last_rssi"]

                    if other_enc.get("min_alt") is not None:
                        enc["min_alt"] = min(enc["min_alt"], other_enc["min_alt"]) if enc["min_alt"] is not None else other_enc["min_alt"]
                    if other_enc.get("max_alt") is not None:
                        enc["max_alt"] = max(enc["max_alt"], other_enc["max_alt"]) if enc["max_alt"] is not None else other_enc["max_alt"]

                    if other_enc.get("min_height") is not None:
                        enc["min_height"] = min(enc["min_height"], other_enc["min_height"]) if enc["min_height"] is not None else other_enc["min_height"]
                    if other_enc.get("max_height") is not None:
                        enc["max_height"] = max(enc["max_height"], other_enc["max_height"]) if enc["max_height"] is not None else other_enc["max_height"]

                    if other_enc.get("min_pressure_alt") is not None:
                        enc["min_pressure_alt"] = min(enc["min_pressure_alt"], other_enc["min_pressure_alt"]) if enc["min_pressure_alt"] is not None else other_enc["min_pressure_alt"]
                    if other_enc.get("max_pressure_alt") is not None:
                        enc["max_pressure_alt"] = max(enc["max_pressure_alt"], other_enc["max_pressure_alt"]) if enc["max_pressure_alt"] is not None else other_enc["max_pressure_alt"]

                    if other_enc.get("max_speed") is not None:
                        enc["max_speed"] = max(enc["max_speed"], other_enc["max_speed"]) if enc["max_speed"] is not None else other_enc["max_speed"]
                    if other_enc.get("max_vert_speed") is not None:
                        enc["max_vert_speed"] = max(enc["max_vert_speed"], other_enc["max_vert_speed"]) if enc["max_vert_speed"] is not None else other_enc["max_vert_speed"]

                    enc["trajectory"].extend(other_enc.get("trajectory", []))
                    self._remove_active(other_enc)
                    if self.db_path:
                        conn_del = None
                        try:
                            conn_del = sqlite3.connect(self.db_path, timeout=30.0)
                            conn_del.execute("DELETE FROM encounters WHERE encounter_id = ?;", (other_enc["encounter_id"],))
                            conn_del.commit()
                        except Exception:
                            pass
                        finally:
                            if conn_del:
                                try:
                                    conn_del.close()
                                except Exception:
                                    pass
                self.active_encounters[mac] = enc

            if serial and is_better_serial(serial, enc.get("serial_number")):
                enc["serial_number"] = sanitize_serial(serial) or serial
                inf = infer_drone_model(enc["serial_number"])
                if inf.get("make"):
                    enc["drone_make"] = inf.get("make")
                if inf.get("model"):
                    enc["drone_model"] = inf.get("model")

            if operator_id and is_better_operator_id(operator_id, enc.get("operator_id")):
                enc["operator_id"] = sanitize_operator_id(operator_id) or operator_id

            if counter is not None:
                enc["counter"] = counter
            if node_id and not enc.get("node_id"):
                enc["node_id"] = node_id
            if ts < enc["first_seen"]:
                enc["first_seen"] = ts
                enc["first_seen_iso"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            if ts > enc["last_seen"]:
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

            if rssi is not None:
                enc["last_rssi"] = rssi
                enc["rssi_count"] += 1
                enc["rssi_sum"] += rssi
                if enc["min_rssi"] is None or rssi < enc["min_rssi"]:
                    enc["min_rssi"] = rssi
                if enc["max_rssi"] is None or rssi > enc["max_rssi"]:
                    enc["max_rssi"] = rssi

            # Parse message telemetry fields
            for msg in msgs_to_process:
                if not isinstance(msg, dict):
                    continue
                if msg.get("is_known_version") is False or (msg.get("protocol_version") is not None and msg.get("protocol_version") not in (0, 1, 2)):
                    continue
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

                    chosen_alt = g_alt if g_alt is not None else alt
                    if chosen_alt is not None:
                        if enc["min_alt"] is None or chosen_alt < enc["min_alt"]:
                            enc["min_alt"] = chosen_alt
                        if enc["max_alt"] is None or chosen_alt > enc["max_alt"]:
                            enc["max_alt"] = chosen_alt

                    if p_alt is not None:
                        if enc["min_pressure_alt"] is None or p_alt < enc["min_pressure_alt"]:
                            enc["min_pressure_alt"] = p_alt
                        if enc["max_pressure_alt"] is None or p_alt > enc["max_pressure_alt"]:
                            enc["max_pressure_alt"] = p_alt

                    if h_m is not None:
                        if enc["min_height"] is None or h_m < enc["min_height"]:
                            enc["min_height"] = h_m
                        if enc["max_height"] is None or h_m > enc["max_height"]:
                            enc["max_height"] = h_m

                    if spd is not None:
                        if enc["max_speed"] is None or spd > enc["max_speed"]:
                            enc["max_speed"] = spd

                    if v_spd is not None:
                        if enc["max_vert_speed"] is None or abs(v_spd) > abs(enc["max_vert_speed"]):
                            enc["max_vert_speed"] = v_spd

                    if lat is not None and lon is not None:
                        # Append 12-element trajectory fix [lat, lon, alt_msl, speed, heading, ts, height_m, height_type, pressure_alt_m, vert_spd, rssi, counter]
                        pt_rssi = rssi if rssi is not None else enc.get("last_rssi")
                        pt_counter = counter if counter is not None else enc.get("counter")
                        enc["trajectory"].append([lat, lon, alt, spd, heading, round(ts, 2), h_m, h_type, p_alt, v_spd, pt_rssi, pt_counter])

                elif m_type == "Basic ID":
                    b_id = msg.get("id")
                    id_t = msg.get("id_type")
                    if b_id and (id_t == 1 or id_t is None):
                        if not enc.get("serial_number") or is_better_serial(b_id, enc.get("serial_number")):
                            enc["serial_number"] = sanitize_serial(b_id) or b_id
                            inf = infer_drone_model(enc["serial_number"])
                            if inf.get("make"):
                                enc["drone_make"] = inf.get("make")
                            if inf.get("model"):
                                enc["drone_model"] = inf.get("model")
                    elif b_id and id_t == 2:
                        if not enc.get("operator_id") or is_better_operator_id(b_id, enc.get("operator_id")):
                            enc["operator_id"] = sanitize_operator_id(b_id) or b_id

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
                    if op_val and (not enc.get("operator_id") or is_better_operator_id(op_val, enc.get("operator_id"))):
                        enc["operator_id"] = sanitize_operator_id(op_val) or op_val

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
            to_delete = []
            seen_eids = set()
            for enc in list(self.active_encounters.values()):
                eid = enc.get("encounter_id")
                if eid and eid not in seen_eids:
                    seen_eids.add(eid)
                    if now - enc["last_seen"] > self.timeout_s:
                        enc["is_active"] = 0
                        self._persist_encounter(enc)
                        closed_ids.append(eid)
                        to_delete.append(enc)
            for enc in to_delete:
                self._remove_active(enc)

            # Periodic WAL checkpoint every 5 minutes
            if now - self.last_wal_checkpoint > 300.0:
                self.last_wal_checkpoint = now
                if self.db_path:
                    conn_chk = None
                    try:
                        conn_chk = sqlite3.connect(self.db_path, timeout=30.0)
                        conn_chk.execute("PRAGMA wal_checkpoint(PASSIVE);")
                    except Exception:
                        pass
                    finally:
                        if conn_chk:
                            try:
                                conn_chk.close()
                            except Exception:
                                pass
        return closed_ids

    def finalize_all(self):
        """Mark all active encounters as completed on shutdown."""
        with self.lock:
            seen_eids = set()
            for enc in list(self.active_encounters.values()):
                eid = enc.get("encounter_id")
                if eid and eid not in seen_eids:
                    seen_eids.add(eid)
                    enc["is_active"] = 0
                    self._persist_encounter(enc)
            self.active_encounters.clear()

    async def update_with_packet_async(self, packet: Dict[str, Any]) -> str:
        """
        Asynchronously updates or creates an active encounter.
        Offloads thread lock acquisition and SQLite persistence to an executor thread
        to prevent blocking the asyncio event loop.
        """
        return await asyncio.to_thread(self.update_with_packet, packet)

    async def finalize_all_async(self) -> None:
        """
        Asynchronously finalizes all active encounters on shutdown.
        Offloads thread lock acquisition and SQLite persistence to an executor thread.
        """
        await asyncio.to_thread(self.finalize_all)

    def _persist_encounter(self, enc: Dict[str, Any]):
        if not self.db_path:
            return

        min_rssi = enc.get("min_rssi")
        max_rssi = enc.get("max_rssi")
        avg_rssi = (
            round(enc["rssi_sum"] / enc["rssi_count"], 1)
            if enc.get("rssi_count", 0) > 0
            else None
        )

        min_alt = enc.get("min_alt")
        max_alt = enc.get("max_alt")
        min_height = enc.get("min_height")
        max_height = enc.get("max_height")
        min_p_alt = enc.get("min_pressure_alt")
        max_p_alt = enc.get("max_pressure_alt")
        max_speed = enc.get("max_speed")

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

        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            try:
                os.makedirs(db_dir, exist_ok=True)
            except Exception:
                pass

        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
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
            logger.error(f"Error persisting encounter to SQLite ({self.db_path}): {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
