#!/usr/bin/env python3
"""
Centralized SQLite Database Manager & Schema Migration Engine for Drone Remote ID Encounters.
Ensures a single canonical source of truth for the SQLite schema, non-destructive column migrations,
and automated CTA-2063-A make/model backfilling.
"""

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

# Ensure repository root is in sys.path for direct script execution
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scanner.drone_models import infer_drone_model
from scanner.timestamp_utils import DEFAULT_MIN_VALID_TIMESTAMP, DEFAULT_MAX_CLOCK_SKEW_S


# Canonical table schema columns (name, SQLite type)
ENCOUNTERS_SCHEMA = [
    ("encounter_id", "TEXT PRIMARY KEY"),
    ("mac", "TEXT NOT NULL"),
    ("serial_number", "TEXT"),
    ("first_seen", "REAL NOT NULL"),
    ("first_seen_iso", "TEXT NOT NULL"),
    ("last_seen", "REAL NOT NULL"),
    ("last_seen_iso", "TEXT NOT NULL"),
    ("duration_s", "REAL NOT NULL"),
    ("packet_count", "INTEGER NOT NULL"),
    ("transports", "TEXT NOT NULL"),
    ("channels", "TEXT NOT NULL"),
    ("wifi_rates", "TEXT"),
    ("dominant_rate_mbps", "REAL"),
    ("dominant_modulation", "TEXT"),
    ("min_rate_mbps", "REAL"),
    ("max_rate_mbps", "REAL"),
    ("phy_rate_dist_json", "TEXT"),
    ("min_rssi_dbm", "INTEGER"),
    ("max_rssi_dbm", "INTEGER"),
    ("avg_rssi_dbm", "REAL"),
    ("min_alt_m", "REAL"),
    ("max_alt_m", "REAL"),
    ("min_height_m", "REAL"),
    ("max_height_m", "REAL"),
    ("min_pressure_alt_m", "REAL"),
    ("max_pressure_alt_m", "REAL"),
    ("max_speed_mps", "REAL"),
    ("pilot_lat", "REAL"),
    ("pilot_lon", "REAL"),
    ("pilot_alt_m", "REAL"),
    ("area_ceil_m", "REAL"),
    ("area_floor_m", "REAL"),
    ("operator_id", "TEXT"),
    ("self_id_desc", "TEXT"),
    ("drone_make", "TEXT"),
    ("drone_model", "TEXT"),
    ("node_id", "TEXT"),
    ("trajectory_json", "TEXT"),
    ("is_active", "INTEGER NOT NULL DEFAULT 1"),
]

# Migration columns that must be added via ALTER TABLE if opening an older database
MIGRATION_COLUMNS = [
    ("min_height_m", "REAL"),
    ("max_height_m", "REAL"),
    ("min_pressure_alt_m", "REAL"),
    ("max_pressure_alt_m", "REAL"),
    ("area_ceil_m", "REAL"),
    ("area_floor_m", "REAL"),
    ("drone_make", "TEXT"),
    ("drone_model", "TEXT"),
    ("node_id", "TEXT"),
    ("wifi_rates", "TEXT"),
    ("dominant_rate_mbps", "REAL"),
    ("dominant_modulation", "TEXT"),
    ("min_rate_mbps", "REAL"),
    ("max_rate_mbps", "REAL"),
    ("phy_rate_dist_json", "TEXT"),
]


def init_encounters_db(conn: sqlite3.Connection, timeout_s: float = 300.0) -> None:
    """
    Initializes the SQLite database:
    1. Sets WAL mode & synchronous = NORMAL for high performance concurrent access.
    2. Creates the canonical encounters table if it does not exist.
    3. Performs non-destructive ALTER TABLE migrations for existing older schema databases.
    4. Automatically backfills drone_make and drone_model for any records with a serial number.
    5. Creates performance indexes.
    6. Reconciles stale active encounters.
    """
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    # 1. Base table creation
    cols_sql = ",\n                    ".join(f"{name} {type_def}" for name, type_def in ENCOUNTERS_SCHEMA)
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS encounters (
            {cols_sql}
        );
    """
    conn.execute(create_table_sql)

    # 1b. Multi-node receiver stations table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receiver_nodes (
            node_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            altitude_m REAL NOT NULL,
            range_rings_json TEXT DEFAULT '[500, 1000, 2500, 5000]',
            description TEXT,
            locked INTEGER NOT NULL DEFAULT 0,
            first_connected_iso TEXT NOT NULL,
            last_heartbeat_epoch REAL NOT NULL,
            last_heartbeat_iso TEXT NOT NULL,
            packets_received_total INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ONLINE'
        );
    """)

    # 1c. Node synchronization state / watermark table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_sync_state (
            node_id TEXT PRIMARY KEY,
            last_synced_epoch REAL NOT NULL DEFAULT 0.0,
            last_synced_iso TEXT NOT NULL,
            packets_synced_total INTEGER NOT NULL DEFAULT 0,
            last_sync_completed_iso TEXT
        );
    """)

    # 2. Non-destructive schema migration for existing SQLite databases
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(encounters);")
    existing_cols = {col[1] for col in cursor.fetchall()}

    for col_name, col_type in MIGRATION_COLUMNS:
        if col_name not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE encounters ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass

    # Check receiver_nodes migrations
    try:
        cursor.execute("PRAGMA table_info(receiver_nodes);")
        node_cols = {col[1] for col in cursor.fetchall()}
        if "locked" not in node_cols:
            conn.execute("ALTER TABLE receiver_nodes ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;")
    except Exception:
        pass

    # 3. Backfill drone_make and drone_model for any existing records with a serial number
    try:
        unfilled = conn.execute(
            "SELECT encounter_id, serial_number, drone_make, drone_model FROM encounters WHERE serial_number IS NOT NULL AND (drone_make IS NULL OR drone_model IS NULL OR drone_model LIKE '%Unspecified%');"
        ).fetchall()
        for r in unfilled:
            # Supports both sqlite3.Row and tuple
            enc_id = r[0]
            s_num = r[1]
            inf = infer_drone_model(s_num)
            if inf.get("is_inferred") and inf.get("model"):
                conn.execute("UPDATE encounters SET drone_make = ?, drone_model = ? WHERE encounter_id = ?;", (inf["make"], inf["model"], enc_id))
    except Exception:
        pass
    # 4. Auto-repair encounters corrupted by drone flight controller claimed system timestamps (< 2026 or future)
    try:
        min_valid_epoch = DEFAULT_MIN_VALID_TIMESTAMP
        max_valid_epoch = time.time() + DEFAULT_MAX_CLOCK_SKEW_S
        corrupted = conn.execute(
            "SELECT encounter_id, first_seen, first_seen_iso, last_seen, last_seen_iso, duration_s FROM encounters WHERE first_seen < ? OR last_seen < ? OR first_seen > ? OR last_seen > ?;",
            (min_valid_epoch, min_valid_epoch, max_valid_epoch, max_valid_epoch)
        ).fetchall()
        for r in corrupted:
            enc_id = r[0]
            f_seen = r[1]
            f_iso = r[2]
            l_seen = r[3]
            l_iso = r[4]
            dur = r[5] or 0.0

            # Recover physical reception timestamp from encounter_id slug (ENC-YYYYMMDD-HHMMSS-XXXXXX)
            parts = enc_id.split("-")
            recovered_ts = None
            if len(parts) >= 4 and len(parts[1]) == 8 and len(parts[2]) == 6:
                try:
                    dt = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                    recovered_ts = dt.timestamp()
                except Exception:
                    pass

            if recovered_ts and (min_valid_epoch <= recovered_ts <= max_valid_epoch):
                new_f_seen = recovered_ts
                new_l_seen = recovered_ts + max(0.0, float(dur))
                new_f_iso = datetime.fromtimestamp(new_f_seen, timezone.utc).isoformat()
                new_l_iso = datetime.fromtimestamp(new_l_seen, timezone.utc).isoformat()
                conn.execute(
                    "UPDATE encounters SET first_seen = ?, first_seen_iso = ?, last_seen = ?, last_seen_iso = ? WHERE encounter_id = ?;",
                    (new_f_seen, new_f_iso, new_l_seen, new_l_iso, enc_id)
                )
    except Exception:
        pass

    # 5. Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_mac ON encounters(mac);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_serial ON encounters(serial_number);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_time ON encounters(first_seen, last_seen);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_active ON encounters(is_active);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_node ON encounters(node_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_status ON receiver_nodes(status);")

    # 6. Auto-reconcile lingering active encounters from prior runs
    now = time.time()
    conn.execute("UPDATE encounters SET is_active = 0 WHERE is_active = 1 AND (? - last_seen) > ?;",
                 (now, timeout_s))
    conn.commit()


def sanitize_serial(s: Optional[str]) -> Optional[str]:
    """Sanitizes serial number by stripping non-alphanumeric noise and truncation."""
    if not s:
        return None
    s_str = str(s).strip()
    for delim in ["@", "\x00", ";", "#", "$"]:
        if delim in s_str:
            s_str = s_str.split(delim)[0]
    cleaned = "".join(c for c in s_str if c.isalnum() or c in "-_")
    if len(cleaned) > 20:
        cleaned = cleaned[:20]
    return cleaned if len(cleaned) >= 6 else None


def sanitize_operator_id(op: Optional[str]) -> Optional[str]:
    """Sanitizes CAA Operator ID by stripping non-alphanumeric noise and truncation."""
    if not op:
        return None
    s_str = str(op).strip()
    for delim in ["\x00", ";", "#", "$"]:
        if delim in s_str:
            s_str = s_str.split(delim)[0]
    cleaned = "".join(c for c in s_str if c.isalnum() or c in "-_")
    # Standard European / CAA ID is 16 chars (e.g. CHEhyolaf9zzbdz0)
    if len(cleaned) > 16 and (cleaned.startswith("CHE") or cleaned.startswith("DEU") or cleaned.startswith("FRA") or cleaned.startswith("AUT") or cleaned.startswith("GBR")):
        cleaned = cleaned[:16]
    return cleaned if len(cleaned) >= 6 else None


def is_fuzzy_mac_match(m1: Optional[str], m2: Optional[str]) -> bool:
    """Matches MAC addresses accounting for BLE RF bit flips or minor noise."""
    if not m1 or not m2:
        return False
    m1_clean = str(m1).strip().upper()
    m2_clean = str(m2).strip().upper()
    if m1_clean == "UNKNOWN" or m2_clean == "UNKNOWN":
        return False
    if m1_clean == m2_clean:
        return True
    if len(m1_clean) == 17 and len(m2_clean) == 17:
        diffs = sum(1 for a, b in zip(m1_clean, m2_clean) if a != b)
        if diffs <= 1:
            return True
    return False


def is_fuzzy_serial_match(s1: Optional[str], s2: Optional[str]) -> bool:
    """Matches serials accounting for BLE RF bit flips or minor noise."""
    if not s1 or not s2:
        return False
    if s1 == s2:
        return True
    # Substring / suffix match (e.g. lost leading char from RF noise: 595B11A20400103 vs 1595B11A20400103)
    if len(s1) >= 10 and len(s2) >= 10:
        if s1 in s2 or s2 in s1:
            return True
        if s1[-10:] == s2[-10:]:
            return True
    if len(s1) == len(s2) and len(s1) >= 12:
        diffs = sum(1 for a, b in zip(s1, s2) if a != b)
        if diffs <= 3:
            return True
    # Subsequence match for dropped or inserted nibbles from RF noise
    if min(len(s1), len(s2)) >= 10:
        mb = sum(b.size for b in SequenceMatcher(None, s1, s2).get_matching_blocks())
        min_len = min(len(s1), len(s2))
        if mb >= 10 and (mb / min_len) >= 0.85:
            return True
    return False


def is_fuzzy_operator_match(op1: Optional[str], op2: Optional[str]) -> bool:
    """Matches operator IDs accounting for BLE RF bit flips."""
    s1 = sanitize_operator_id(op1)
    s2 = sanitize_operator_id(op2)
    if not s1 or not s2:
        return False
    if s1 == s2:
        return True
    if len(s1) == len(s2) and len(s1) >= 12:
        diffs = sum(1 for a, b in zip(s1, s2) if a != b)
        if diffs <= 3:
            return True
    return False


def is_better_serial(new_s: Optional[str], old_s: Optional[str]) -> bool:
    """Determines whether new_s is a higher quality/canonical serial than old_s."""
    if not old_s:
        return True
    if not new_s:
        return False
    known_prefixes = ("1581F", "1595B", "1596E", "1752E")
    n_qual = sum([
        14 <= len(new_s) <= 20,
        any(new_s.startswith(p) for p in known_prefixes),
        new_s.isalnum()
    ])
    o_qual = sum([
        14 <= len(old_s) <= 20,
        any(old_s.startswith(p) for p in known_prefixes),
        old_s.isalnum()
    ])
    return n_qual > o_qual


def is_better_operator_id(new_op: Optional[str], old_op: Optional[str]) -> bool:
    """Determines whether new_op is a higher quality/canonical operator ID than old_op."""
    if not old_op:
        return True
    if not new_op:
        return False
    s_new = sanitize_operator_id(new_op) or ""
    s_old = sanitize_operator_id(old_op) or ""
    known_prefixes = ("CHE", "DEU", "FRA", "AUT", "GBR")
    n_qual = sum([
        len(s_new) == 16,
        any(s_new.startswith(p) for p in known_prefixes),
        s_new.isalnum()
    ])
    o_qual = sum([
        len(s_old) == 16,
        any(s_old.startswith(p) for p in known_prefixes),
        s_old.isalnum()
    ])
    return n_qual > o_qual


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two coordinates in meters."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * R * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _merge_cluster_records(records: List[sqlite3.Row]) -> Tuple:
    """
    Merges a cluster of sequential encounter records belonging to the same drone into a single canonical row tuple.
    """
    f_seen = min(r["first_seen"] for r in records)
    l_seen = max(r["last_seen"] for r in records)
    f_iso = datetime.fromtimestamp(f_seen, timezone.utc).isoformat()
    l_iso = datetime.fromtimestamp(l_seen, timezone.utc).isoformat()
    dur = round(l_seen - f_seen, 2)
    pkts = sum(r["packet_count"] or 0 for r in records)

    # Transports, channels, wifi rates
    t_set = set()
    c_set = set()
    wr_set = set()
    for r in records:
        if r["transports"]:
            t_set.update(filter(None, r["transports"].split(",")))
        if r["channels"]:
            c_set.update(filter(None, r["channels"].split(",")))
        if r["wifi_rates"]:
            wr_set.update(filter(None, [x.strip() for x in r["wifi_rates"].split(",") if x.strip()]))

    transports = ",".join(sorted(t_set))
    channels = ",".join(sorted(c_set))
    wifi_rates = ", ".join(sorted(wr_set)) if wr_set else None

    # Merge PHY distributions
    merged_phy = {}
    for r in records:
        if r["phy_rate_dist_json"]:
            try:
                p = json.loads(r["phy_rate_dist_json"])
                for k, v in p.items():
                    if k in merged_phy:
                        merged_phy[k]["count"] = merged_phy[k].get("count", 0) + v.get("count", 0)
                    else:
                        merged_phy[k] = dict(v)
            except Exception:
                pass
    phy_rate_dist_json = json.dumps(merged_phy) if merged_phy else None

    dominant_rate_mbps = None
    dominant_modulation = None
    min_rate_mbps = None
    max_rate_mbps = None
    if merged_phy:
        sorted_entries = sorted(merged_phy.items(), key=lambda x: x[1].get("count", 0), reverse=True)
        if sorted_entries:
            dom_info = sorted_entries[0][1]
            dominant_rate_mbps = dom_info.get("rate_mbps")
            dominant_modulation = dom_info.get("modulation")
        all_r = [v["rate_mbps"] for v in merged_phy.values() if v.get("rate_mbps") is not None]
        if all_r:
            min_rate_mbps = min(all_r)
            max_rate_mbps = max(all_r)

    # RSSI
    r_mins = [r["min_rssi_dbm"] for r in records if r["min_rssi_dbm"] is not None]
    r_maxs = [r["max_rssi_dbm"] for r in records if r["max_rssi_dbm"] is not None]
    min_rssi = min(r_mins) if r_mins else None
    max_rssi = max(r_maxs) if r_maxs else None

    total_pkts_rssi = 0
    sum_rssi = 0.0
    for r in records:
        if r["avg_rssi_dbm"] is not None:
            c = r["packet_count"] or 1
            sum_rssi += r["avg_rssi_dbm"] * c
            total_pkts_rssi += c
    avg_rssi = round(sum_rssi / total_pkts_rssi, 1) if total_pkts_rssi > 0 else None

    def _min_field(fname):
        vals = [r[fname] for r in records if r[fname] is not None]
        return min(vals) if vals else None

    def _max_field(fname):
        vals = [r[fname] for r in records if r[fname] is not None]
        return max(vals) if vals else None

    min_alt = _min_field("min_alt_m")
    max_alt = _max_field("max_alt_m")
    min_h = _min_field("min_height_m")
    max_h = _max_field("max_height_m")
    min_p = _min_field("min_pressure_alt_m")
    max_p = _max_field("max_pressure_alt_m")
    max_spd = _max_field("max_speed_mps")

    # Deduplicated trajectory merge
    combined_traj = []
    for r in records:
        if r["trajectory_json"]:
            try:
                t = json.loads(r["trajectory_json"])
                if isinstance(t, list):
                    combined_traj.extend(t)
            except Exception:
                pass
    seen_pts = set()
    deduped_traj = []
    for pt in combined_traj:
        if isinstance(pt, (list, tuple)) and len(pt) >= 6:
            key = (round(pt[0], 5), round(pt[1], 5), round(pt[5], 1))
            if key not in seen_pts:
                seen_pts.add(key)
                deduped_traj.append(pt)
    deduped_traj.sort(key=lambda p: p[5] if len(p) >= 6 else 0)
    traj_json = json.dumps(deduped_traj) if deduped_traj else None

    # Canonical serial selection
    s_cands = []
    for r in records:
        if r["serial_number"]:
            san = sanitize_serial(r["serial_number"])
            if san:
                s_cands.append(san)
            s_cands.append(r["serial_number"])
    serial = None
    for cand in s_cands:
        if is_better_serial(cand, serial):
            serial = cand

    drone_make = None
    drone_model = None
    for r in records:
        if not drone_make and r["drone_make"]:
            drone_make = r["drone_make"]
        if not drone_model and r["drone_model"]:
            drone_model = r["drone_model"]
    if serial:
        inf = infer_drone_model(serial)
        if inf.get("make"):
            drone_make = inf["make"]
        if inf.get("model"):
            drone_model = inf["model"]

    # Canonical operator ID selection
    op_cands = []
    for r in records:
        if r["operator_id"]:
            san = sanitize_operator_id(r["operator_id"])
            if san:
                op_cands.append(san)
            op_cands.append(r["operator_id"])
    operator_id = None
    for cand in op_cands:
        if is_better_operator_id(cand, operator_id):
            operator_id = cand

    self_id_desc = next((r["self_id_desc"] for r in records if r["self_id_desc"]), None)
    pilot_lat = next((r["pilot_lat"] for r in records if r["pilot_lat"] is not None), None)
    pilot_lon = next((r["pilot_lon"] for r in records if r["pilot_lon"] is not None), None)
    pilot_alt = next((r["pilot_alt_m"] for r in records if r["pilot_alt_m"] is not None), None)
    area_ceil = next((r["area_ceil_m"] for r in records if r["area_ceil_m"] is not None), None)
    area_floor = next((r["area_floor_m"] for r in records if r["area_floor_m"] is not None), None)
    node_id = next((r["node_id"] for r in records if r["node_id"]), None)
    is_active = max((r["is_active"] or 0 for r in records), default=0)

    # Prefer non-1970 encounter_id as primary target
    corrupt_prefixes = ("ENC-1970", "ENC-1972", "ENC-1995", "ENC-2060", "ENC-2061")
    final_id = records[0]["encounter_id"]
    for r in records:
        eid = r["encounter_id"]
        if not any(eid.startswith(p) for p in corrupt_prefixes):
            final_id = eid
            break

    primary_mac = "UNKNOWN"
    for r in records:
        if r["mac"] and r["mac"] != "UNKNOWN":
            primary_mac = r["mac"]
            break

    return (
        final_id, primary_mac, serial, f_seen, f_iso, l_seen, l_iso, dur, pkts, transports,
        channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps, max_rate_mbps,
        phy_rate_dist_json, min_rssi, max_rssi, avg_rssi, min_alt, max_alt, min_h, max_h, min_p,
        max_p, max_spd, pilot_lat, pilot_lon, pilot_alt, area_ceil, area_floor, operator_id,
        self_id_desc, drone_make, drone_model, node_id, traj_json, is_active
    )


def merge_sequential_encounters(conn: sqlite3.Connection, timeout_s: float = 300.0) -> int:
    """
    No-op: heuristic encounter merging has been retired.
    Maintained for backwards-compatibility with callers/scripts.
    """
    return 0


def get_db_connection(db_path: str, timeout_s: float = 300.0) -> sqlite3.Connection:
    """
    Returns a fast SQLite connection configured with Row factory, WAL mode,
    and synchronous = NORMAL. Only initializes the schema if the encounters
    table does not yet exist.
    """
    conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    # Fast check: only run full schema migrations/indexes on first creation
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='encounters' LIMIT 1;")
    if cur.fetchone() is None:
        init_encounters_db(conn, timeout_s=timeout_s)
    return conn


def reconcile_stale_encounters(conn: sqlite3.Connection, timeout_s: float = 300.0) -> None:
    """Auto-closes any active encounters whose last packet is older than timeout_s."""
    try:
        now = time.time()
        conn.execute(
            "UPDATE encounters SET is_active = 0 WHERE is_active = 1 AND (? - last_seen) > ?;",
            (now, timeout_s)
        )
        conn.commit()
    except Exception:
        pass


def upsert_receiver_node(
    conn: sqlite3.Connection,
    node_id: str,
    name: str,
    latitude: float,
    longitude: float,
    altitude_m: float,
    range_rings_json: Optional[str] = None,
    description: Optional[str] = None,
    locked: bool = False,
    packets_increment: int = 0,
    status: str = "ONLINE",
) -> None:
    """Registers or updates a receiver station in the receiver_nodes table."""
    now = time.time()
    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    rings = range_rings_json or "[500, 1000, 2500, 5000]"
    locked_int = 1 if locked else 0

    conn.execute("""
        INSERT INTO receiver_nodes (
            node_id, name, latitude, longitude, altitude_m,
            range_rings_json, description, locked, first_connected_iso,
            last_heartbeat_epoch, last_heartbeat_iso, packets_received_total, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            name = excluded.name,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            altitude_m = excluded.altitude_m,
            range_rings_json = excluded.range_rings_json,
            description = COALESCE(excluded.description, receiver_nodes.description),
            locked = excluded.locked,
            last_heartbeat_epoch = excluded.last_heartbeat_epoch,
            last_heartbeat_iso = excluded.last_heartbeat_iso,
            packets_received_total = receiver_nodes.packets_received_total + excluded.packets_received_total,
            status = excluded.status;
    """, (
        node_id, name, latitude, longitude, altitude_m,
        rings, description, locked_int, iso_now, now, iso_now, packets_increment, status
    ))
    conn.commit()


def touch_receiver_node_heartbeat(
    conn: sqlite3.Connection,
    node_id: str,
    packets_increment: int = 0,
) -> None:
    """Refreshes the heartbeat timestamp and increments packet count for a receiver node."""
    now = time.time()
    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    conn.execute("""
        UPDATE receiver_nodes SET
            last_heartbeat_epoch = ?,
            last_heartbeat_iso = ?,
            packets_received_total = packets_received_total + ?,
            status = 'ONLINE'
        WHERE node_id = ?;
    """, (now, iso_now, packets_increment, node_id))
    conn.commit()


def update_receiver_node_position(
    conn: sqlite3.Connection,
    node_id: str,
    latitude: float,
    longitude: float,
    altitude_m: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Updates the physical coordinates of an unlocked receiver node in the database.
    Raises PermissionError if the node is locked on disk (locked == 1).
    """
    row = conn.execute("SELECT * FROM receiver_nodes WHERE node_id = ?;", (node_id,)).fetchone()
    if not row:
        raise KeyError(f"Receiver node '{node_id}' not found in database.")
    
    node = dict(row)
    if bool(node.get("locked")):
        raise PermissionError(f"Receiver node '{node_id}' is locked on disk via scanner_config.json.")

    if altitude_m is not None:
        conn.execute(
            "UPDATE receiver_nodes SET latitude = ?, longitude = ?, altitude_m = ? WHERE node_id = ?;",
            (latitude, longitude, altitude_m, node_id)
        )
    else:
        conn.execute(
            "UPDATE receiver_nodes SET latitude = ?, longitude = ? WHERE node_id = ?;",
            (latitude, longitude, node_id)
        )
    conn.commit()

    updated = conn.execute("SELECT * FROM receiver_nodes WHERE node_id = ?;", (node_id,)).fetchone()
    return dict(updated)


def get_receiver_nodes(conn: sqlite3.Connection, offline_timeout_s: float = 60.0) -> List[Dict[str, Any]]:
    """Retrieves all registered receiver nodes, dynamically updating status based on last heartbeat."""
    now = time.time()
    rows = conn.execute("SELECT * FROM receiver_nodes ORDER BY name ASC;").fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["locked"] = bool(d.get("locked", 0))
        last_hb = float(d.get("last_heartbeat_epoch", 0.0))
        if now - last_hb > (offline_timeout_s * 3):
            d["status"] = "OFFLINE"
        elif now - last_hb > offline_timeout_s:
            d["status"] = "DEGRADED"
        else:
            d["status"] = "ONLINE"
        results.append(d)
    return results


def get_node_sync_watermark(conn: sqlite3.Connection, node_id: str) -> float:
    """Returns the last synchronized epoch timestamp for a given node, or 0.0 if not found."""
    row = conn.execute("SELECT last_synced_epoch FROM node_sync_state WHERE node_id = ?;", (node_id,)).fetchone()
    if row:
        return float(row[0])
    return 0.0


def update_node_sync_watermark(conn: sqlite3.Connection, node_id: str, last_epoch: float, packets_synced_count: int = 0) -> None:
    """Updates the synchronization watermark for a node."""
    iso_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_epoch))
    now = time.time()
    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    conn.execute("""
        INSERT INTO node_sync_state (
            node_id, last_synced_epoch, last_synced_iso, packets_synced_total, last_sync_completed_iso
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            last_synced_epoch = MAX(node_sync_state.last_synced_epoch, excluded.last_synced_epoch),
            last_synced_iso = excluded.last_synced_iso,
            packets_synced_total = node_sync_state.packets_synced_total + excluded.packets_synced_total,
            last_sync_completed_iso = excluded.last_sync_completed_iso;
    """, (node_id, last_epoch, iso_str, packets_synced_count, iso_now))
    conn.commit()
