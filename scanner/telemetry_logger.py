#!/usr/bin/env python3
"""
Unified Telemetry Logger & Terminal Interface
Consumes parsed RID events and manages:
1. Colorized live console display (or periodic quiet heartbeat in daemon mode).
2. Append-only replay-compatible JSONL log with optional daily date splitting.
3. SQLite 5-minute flight encounter grouping & throttled persistence.
4. Distributed streaming via CentralStreamForwarder (3-tier reliability).
5. Daily multi-node PCAP logging integration.
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from scanner.db import touch_receiver_node_heartbeat, upsert_receiver_node
from scanner.drone_models import infer_drone_model
from scanner.encounter_tracker import EncounterTracker
from scanner.pcap_logger import DailyNodePcapLogger

logger = logging.getLogger("CombinedRIDListener.TelemetryLogger")

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
        log_pcap: Optional[str] = None,
        pcap_dir: Optional[str] = None,
    ):
        self.forwarder = forwarder
        self.node_id = node_id
        self.node_meta = node_meta or {}
        self.base_log_path = log_jsonl_path
        self.rotate_daily = rotate_daily
        self.quiet = quiet
        self.current_log_path: Optional[str] = None
        self.log_file_handle = None

        # PCAP Logging (Daily rotated per node: 1 file for Bluetooth, 1 file for Wi-Fi)
        self.pcap_logger: Optional[DailyNodePcapLogger] = None
        if log_pcap or pcap_dir:
            if pcap_dir:
                resolved_pcap_dir = pcap_dir
                pcap_prefix = (
                    log_pcap
                    if (log_pcap and not os.path.isdir(log_pcap) and "/" not in log_pcap and "\\" not in log_pcap)
                    else ""
                )
            elif log_pcap:
                if os.path.isdir(log_pcap) or log_pcap.endswith(("/", "\\")):
                    resolved_pcap_dir = log_pcap
                    pcap_prefix = ""
                elif "/" in log_pcap or "\\" in log_pcap:
                    resolved_pcap_dir = os.path.dirname(os.path.abspath(log_pcap))
                    pcap_prefix = os.path.basename(log_pcap)
                else:
                    resolved_pcap_dir = log_pcap
                    pcap_prefix = ""
            else:
                resolved_pcap_dir = "pcaps"
                pcap_prefix = ""

            self.pcap_logger = DailyNodePcapLogger(
                base_dir=resolved_pcap_dir,
                node_id=self.node_id or "node",
                rotate_daily=True,
                file_prefix=pcap_prefix,
                quiet=quiet,
            )

        self.encounter_tracker = (
            EncounterTracker(
                db_path=db_path,
                timeout_s=encounter_timeout_s,
                persist_interval_s=persist_interval_s,
                default_node_id=self.node_id,
            )
            if db_path
            else None
        )

        # Register local node in database if local SQLite DB active
        if db_path and self.node_id:
            conn_reg = None
            try:
                conn_reg = sqlite3.connect(db_path, timeout=30.0)
                upsert_receiver_node(
                    conn_reg,
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
            finally:
                if conn_reg:
                    try:
                        conn_reg.close()
                    except Exception:
                        pass

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
                    print(
                        f"{C_GRAY}[*] Flight Encounter {c_id} closed "
                        f"({self.encounter_tracker.timeout_s:.0f}s silence timeout).{C_RESET}"
                    )
            self.last_timeout_check = now

        # 2. Touch local receiver node heartbeat in SQLite every 15s
        if self.encounter_tracker and self.encounter_tracker.db_path and self.node_id:
            if now - self.last_node_heartbeat >= 15.0:
                self.last_node_heartbeat = now
                conn_hb = None
                try:
                    conn_hb = sqlite3.connect(self.encounter_tracker.db_path, timeout=30.0)
                    touch_receiver_node_heartbeat(conn_hb, self.node_id)
                except Exception:
                    pass
                finally:
                    if conn_hb:
                        try:
                            conn_hb.close()
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
            print(
                f"[STATUS {iso_str}] Total: {self.stats['total_packets']} pkts (BLE: {bt_cnt} [BT5: {bt5_cnt}, "
                f"BT4: {bt4_cnt}], Wi-Fi: {wifi_cnt}) | Active Encounters: {active_cnt} | "
                f"Unique Drones: {len(self.stats['macs'])}{hub_str}"
            )
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
            ch_key = (
                f"Ch {ch_raw}"
                if (isinstance(ch_raw, int) or (isinstance(ch_raw, str) and ch_raw.isdigit()))
                else str(ch_raw or "Adv")
            )
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

        # 4. Write to Daily PCAP (1 for Wi-Fi, 1 for BLE per node) (if configured)
        if self.pcap_logger:
            self.pcap_logger.log_event(event)

        # In quiet mode, per-packet display is suppressed (heartbeats handled in periodic_maintenance)
        if self.quiet:
            return

        # 5. Format Live Console Banner
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
        print(
            f"   {C_WHITE}MAC: {C_BOLD}{mac}{C_RESET} | {C_WHITE}RSSI: {C_BOLD}{rssi_str}{C_RESET} | "
            f"{C_WHITE}RF: {C_MAGENTA}{rf_info}{C_RESET}"
        )
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
                print(
                    f"   {C_YELLOW}🆔 Basic ID  {C_RESET}{ver_tag} -> ID: {b_id} | Type: {id_type_name} | "
                    f"Aircraft: {ua_type_name}"
                )

            elif m_type == "System":
                p_lat = msg.get("pilot_lat")
                p_lon = msg.get("pilot_lon")
                p_alt = msg.get("pilot_alt_m")
                radius = msg.get("area_radius_m")
                op_loc_type = msg.get("operator_location_type_name", "")

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
                    auth_len = msg.get("auth_data_length", len(auth_hex) // 2)
                    print(
                        f"   {C_BLUE}🔒 Auth      {C_RESET}{ver_tag} -> Type: {auth_type_name} | "
                        f"Page: 0/{msg.get('last_page_index', 0)} (Len: {auth_len}B) | Hex: {auth_hex[:32]}..."
                    )
                else:
                    print(
                        f"   {C_BLUE}🔒 Auth      {C_RESET}{ver_tag} -> Type: {auth_type_name} | "
                        f"Page: {page_num} | Hex: {auth_hex[:32]}..."
                    )

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
        if self.pcap_logger:
            self.pcap_logger.close()

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

        if self.pcap_logger:
            p_stats = self.pcap_logger.get_stats()
            print(f"  • Daily PCAP Captures (Node: {p_stats['node_id']}):")
            wifi_file = os.path.basename(p_stats["current_wifi_file"]) if p_stats.get("current_wifi_file") else "None"
            ble_file = os.path.basename(p_stats["current_ble_file"]) if p_stats.get("current_ble_file") else "None"
            print(f"      - Wi-Fi PCAP  : {p_stats['wifi_packets']} pkts ({p_stats['wifi_bytes']} bytes) -> {wifi_file}")
            print(f"      - BLE PCAP    : {p_stats['ble_packets']} pkts ({p_stats['ble_bytes']} bytes) -> {ble_file}")

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
