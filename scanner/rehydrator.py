#!/usr/bin/env python3
"""
JSONL Historical Log Rehydration
Retroactively parses raw base64 ASTM messages in JSONL log files (rid_packets_*.jsonl)
and re-populates/upgrades the SQLite encounter database with full trajectory fixes,
min/max heights, pressure altitudes, system telemetry limits, and node_id attribution.
"""

import base64
import glob
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from scanner.encounter_tracker import EncounterTracker
from scanner.parser import decode_astm_message, parse_astm_payload
from scanner.scanner_config import load_scanner_config
from scanner.timestamp_utils import resolve_reception_timestamp

logger = logging.getLogger("CombinedRIDListener.Rehydrator")


def rehydrate_db_from_jsonl(
    db_path: str = "rid_detections.db",
    log_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    reset_db: bool = False,
) -> int:
    """
    Retroactively parses all raw base64 ASTM messages in JSONL log files (rid_packets_*.jsonl)
    and re-populates/upgrades the SQLite encounter database with full trajectory fixes,
    min/max heights, pressure altitudes, system telemetry limits, and node_id attribution.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    if node_id is None:
        try:
            cfg = load_scanner_config()
            node_id = cfg.get("node_id", "sensor-node-01")
        except Exception:
            node_id = "sensor-node-01"

    if log_dir in (None, ".", ""):
        search_patterns = [
            "rid_packets_*.jsonl",
            "rid_packets*.jsonl",
            "*.jsonl",
            "central_logs/rid_packets*.jsonl",
            "central_logs/*.jsonl",
            os.path.join(repo_root, "rid_packets*.jsonl"),
            os.path.join(repo_root, "*.jsonl"),
            os.path.join(repo_root, "central_logs", "rid_packets*.jsonl"),
            os.path.join(repo_root, "central_logs", "*.jsonl"),
            os.path.join(os.path.dirname(__file__), "..", "rid_packets*.jsonl"),
            os.path.join(os.path.dirname(__file__), "..", "*.jsonl"),
            os.path.join(os.path.dirname(__file__), "..", "central_logs", "rid_packets*.jsonl"),
            os.path.join(os.path.dirname(__file__), "..", "central_logs", "*.jsonl"),
            os.path.join(os.path.dirname(__file__), "rid_packets*.jsonl"),
            os.path.join(os.path.dirname(__file__), "*.jsonl"),
        ]
    else:
        search_patterns = [
            os.path.join(log_dir, "rid_packets_*.jsonl"),
            os.path.join(log_dir, "rid_packets*.jsonl"),
            os.path.join(log_dir, "*.jsonl"),
            os.path.join(log_dir, "central_logs", "rid_packets*.jsonl"),
            os.path.join(log_dir, "central_logs", "*.jsonl"),
        ]

    if db_path == "rid_detections.db":
        if os.path.isfile("rid_detections_central.db") and not os.path.isfile("rid_detections.db"):
            db_path = "rid_detections_central.db"
        elif os.path.isfile(os.path.join(repo_root, "rid_detections_central.db")) and not os.path.isfile(os.path.join(repo_root, "rid_detections.db")):
            db_path = os.path.join(repo_root, "rid_detections_central.db")

    log_files = []
    for pattern in search_patterns:
        for p in glob.glob(pattern):
            abs_p = os.path.abspath(p)
            if abs_p.endswith(".bak") or ".bak." in abs_p:
                continue
            if abs_p not in log_files and os.path.isfile(abs_p):
                log_files.append(abs_p)

    if not log_files:
        logger.info("[*] Rehydration: No JSONL packet log files found.")
        return 0

    logger.info(f"[*] Rehydration: Found {len(log_files)} packet log file(s): {[os.path.basename(f) for f in log_files]}")

    all_packets: List[Dict[str, Any]] = []
    for fpath in sorted(log_files):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
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
        return resolve_reception_timestamp(p)

    # Sort all packets chronologically so encounters build accurately over time
    all_packets.sort(key=_resolve_ts)

    if reset_db and db_path and os.path.exists(db_path):
        conn_rst = None
        try:
            conn_rst = sqlite3.connect(db_path, timeout=30.0)
            conn_rst.execute("DELETE FROM encounters;")
            conn_rst.commit()
            logger.info(f"[*] Cleared existing encounters in {db_path} prior to rehydration (--reset).")
        except Exception as e:
            logger.warning(f"[-] Could not clear encounters prior to rehydration: {e}")
        finally:
            if conn_rst:
                try:
                    conn_rst.close()
                except Exception:
                    pass

    try:
        tracker = EncounterTracker(db_path=db_path, persist_interval_s=0.0, default_node_id=node_id)
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower() or "permission" in str(e).lower():
            logger.error(
                f"[-] Database permission error opening '{db_path}': {e}\n"
                f"    -> The database was likely created by root/sudo. Run rehydration with sudo:\n"
                f"       sudo .venv/bin/python3 scanner/combined_rid_listener.py --rehydrate\n"
                f"    -> Or fix file ownership with:\n"
                f"       sudo chown -R $USER:$USER .\n"
            )
            return 0
        raise

    touched_encounters = set()
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
        mac = p.get("mac", "UNKNOWN")
        serial = p.get("serial_number") if p.get("serial_number") is not None else p.get("serial")
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
        eid = tracker.update_with_packet(pkt_obj)
        if eid:
            touched_encounters.add(eid)

    tracker.finalize_all()

    rehydrated_count = len(touched_encounters)
    logger.info(f"[+] Rehydration complete: {rehydrated_count} encounter(s) updated in {db_path}")
    return rehydrated_count


rehydrate_database = rehydrate_db_from_jsonl


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Drone Remote ID Database Rehydrator from JSONL logs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db-file", type=str, default="rid_detections.db", help="Path to SQLite DB")
    parser.add_argument("--log-dir", type=str, default=None, help="Directory containing JSONL log files")
    parser.add_argument("--node-id", type=str, default=None, help="Fallback sensor node ID")
    parser.add_argument("--central", action="store_true", help="Shortcut for central hub preset (--db-file rid_detections_central.db --log-dir central_logs)")
    parser.add_argument("--reset", action="store_true", help="Clear existing encounters in database before rehydrating from JSONL")

    args = parser.parse_args()

    target_db = "rid_detections_central.db" if args.central else args.db_file
    target_dir = "central_logs" if args.central else args.log_dir

    print(f"[*] Starting rehydration: DB={target_db}, log_dir={target_dir}, reset={args.reset}")
    count = rehydrate_db_from_jsonl(db_path=target_db, log_dir=target_dir, node_id=args.node_id, reset_db=args.reset)
    print(f"[+] Rehydration completed: {count} encounters processed.")


if __name__ == "__main__":
    main()
