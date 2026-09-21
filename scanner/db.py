#!/usr/bin/env python3
"""
Centralized SQLite Database Manager & Schema Migration Engine for Drone Remote ID Encounters.
Ensures a single canonical source of truth for the SQLite schema, non-destructive column migrations,
and automated CTA-2063-A make/model backfilling.
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure repo and scanner paths in sys.path
scanner_dir = os.path.abspath(os.path.dirname(__file__))
repo_root = os.path.abspath(os.path.join(scanner_dir, ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
if scanner_dir not in sys.path:
    sys.path.insert(0, scanner_dir)

try:
    from scanner.drone_models import infer_drone_model
except ImportError:
    try:
        from drone_models import infer_drone_model
    except ImportError:
        def infer_drone_model(serial: Optional[str]) -> Dict[str, Any]:
            return {"make": None, "model": None, "company": None, "country": None, "is_inferred": False}


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
    # 4. Auto-repair encounters corrupted by drone flight controller claimed system timestamps (< 2023 or future like 2061)
    try:
        max_valid_epoch = 2000000000.0  # May 18, 2033 UTC (catches uncalibrated hardware tick overflows like 2061)
        min_valid_epoch = 1700000000.0        # Nov 2023
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

    # 4b. Merge sequential split encounters by Serial number or MAC address within timeout window
    try:
        merge_sequential_encounters(conn, timeout_s=timeout_s)
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


def merge_sequential_encounters(conn: sqlite3.Connection, timeout_s: float = 300.0) -> int:
    """
    Scans the database and merges sequential flight encounters that belong to the same drone
    (matching serial number or matching MAC address) within the inactivity timeout window (default: 300s).
    Unifies durations, packet counts, transports, channels, PHY distributions, RSSI, telemetry, and trajectories.
    """
    conn.row_factory = sqlite3.Row
    merged_total = 0

    while True:
        rows = conn.execute("""
            SELECT * FROM encounters
            ORDER BY first_seen ASC, last_seen ASC
        """).fetchall()

        # Build lookup maps by serial and by mac
        serials_map: Dict[str, List[Any]] = {}
        macs_map: Dict[str, List[Any]] = {}
        for r in rows:
            s = r["serial_number"]
            m = r["mac"]
            if s:
                serials_map.setdefault(s, []).append(r)
            elif m and m != "UNKNOWN":
                macs_map.setdefault(m, []).append(r)

        candidate = None

        # Check serial matches first
        for s, elist in serials_map.items():
            if len(elist) > 1:
                for i in range(len(elist) - 1):
                    e1, e2 = elist[i], elist[i + 1]
                    if e2["first_seen"] >= e1["first_seen"] - 60.0 and (e2["first_seen"] - e1["last_seen"] <= timeout_s):
                        candidate = (e1, e2)
                        break
                if candidate:
                    break

        if not candidate:
            for m, elist in macs_map.items():
                if len(elist) > 1:
                    for i in range(len(elist) - 1):
                        e1, e2 = elist[i], elist[i + 1]
                        if e2["first_seen"] >= e1["first_seen"] - 60.0 and (e2["first_seen"] - e1["last_seen"] <= timeout_s):
                            candidate = (e1, e2)
                            break
                    if candidate:
                        break

        if not candidate:
            break

        e1, e2 = candidate
        t_id = e1["encounter_id"]
        s_id = e2["encounter_id"]
        if t_id == s_id:
            break

        f_seen = min(e1["first_seen"], e2["first_seen"])
        l_seen = max(e1["last_seen"], e2["last_seen"])
        f_iso = datetime.fromtimestamp(f_seen, timezone.utc).isoformat()
        l_iso = datetime.fromtimestamp(l_seen, timezone.utc).isoformat()
        dur = round(l_seen - f_seen, 2)
        pkts = (e1["packet_count"] or 0) + (e2["packet_count"] or 0)

        # Transports and channels
        t_set = set(filter(None, (e1["transports"] or "").split(",") + (e2["transports"] or "").split(",")))
        c_set = set(filter(None, (e1["channels"] or "").split(",") + (e2["channels"] or "").split(",")))
        transports = ",".join(sorted(t_set))
        channels = ",".join(sorted(c_set))

        # Wi-Fi rates and PHY distribution
        wr_set = set(filter(None, [r.strip() for r in (e1["wifi_rates"] or "").split(",") if r.strip()] + [r.strip() for r in (e2["wifi_rates"] or "").split(",") if r.strip()]))
        wifi_rates = ", ".join(sorted(wr_set)) if wr_set else None

        phy1, phy2 = {}, {}
        try:
            if e1["phy_rate_dist_json"]:
                phy1 = json.loads(e1["phy_rate_dist_json"])
        except Exception:
            pass
        try:
            if e2["phy_rate_dist_json"]:
                phy2 = json.loads(e2["phy_rate_dist_json"])
        except Exception:
            pass

        merged_phy = dict(phy1)
        for k, v in phy2.items():
            if k in merged_phy:
                merged_phy[k]["count"] = merged_phy[k].get("count", 0) + v.get("count", 0)
            else:
                merged_phy[k] = v
        phy_rate_dist_json = json.dumps(merged_phy) if merged_phy else None

        # RSSI
        r_mins = [v for v in [e1["min_rssi_dbm"], e2["min_rssi_dbm"]] if v is not None]
        r_maxs = [v for v in [e1["max_rssi_dbm"], e2["max_rssi_dbm"]] if v is not None]
        min_rssi = min(r_mins) if r_mins else None
        max_rssi = max(r_maxs) if r_maxs else None
        avg_rssi = None
        if e1["avg_rssi_dbm"] is not None and e2["avg_rssi_dbm"] is not None:
            c1, c2 = e1["packet_count"] or 1, e2["packet_count"] or 1
            avg_rssi = round((e1["avg_rssi_dbm"] * c1 + e2["avg_rssi_dbm"] * c2) / (c1 + c2), 1)
        elif e1["avg_rssi_dbm"] is not None:
            avg_rssi = e1["avg_rssi_dbm"]
        else:
            avg_rssi = e2["avg_rssi_dbm"]

        def _min_val(a, b):
            v = [x for x in [a, b] if x is not None]
            return min(v) if v else None

        def _max_val(a, b):
            v = [x for x in [a, b] if x is not None]
            return max(v) if v else None

        min_alt = _min_val(e1["min_alt_m"], e2["min_alt_m"])
        max_alt = _max_val(e1["max_alt_m"], e2["max_alt_m"])
        min_h = _min_val(e1["min_height_m"], e2["min_height_m"])
        max_h = _max_val(e1["max_height_m"], e2["max_height_m"])
        min_p = _min_val(e1["min_pressure_alt_m"], e2["min_pressure_alt_m"])
        max_p = _max_val(e1["max_pressure_alt_m"], e2["max_pressure_alt_m"])
        max_spd = _max_val(e1["max_speed_mps"], e2["max_speed_mps"])

        # Trajectory merge
        traj1, traj2 = [], []
        try:
            if e1["trajectory_json"]:
                traj1 = json.loads(e1["trajectory_json"])
        except Exception:
            pass
        try:
            if e2["trajectory_json"]:
                traj2 = json.loads(e2["trajectory_json"])
        except Exception:
            pass
        combined_traj = traj1 + traj2
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

        serial = e1["serial_number"] or e2["serial_number"]
        drone_make = e1["drone_make"] or e2["drone_make"]
        drone_model = e1["drone_model"] or e2["drone_model"]
        if serial and (not drone_make or not drone_model):
            inf = infer_drone_model(serial)
            drone_make = drone_make or inf.get("make")
            drone_model = drone_model or inf.get("model")

        operator_id = e1["operator_id"] or e2["operator_id"]
        self_id_desc = e1["self_id_desc"] or e2["self_id_desc"]
        pilot_lat = e1["pilot_lat"] if e1["pilot_lat"] is not None else e2["pilot_lat"]
        pilot_lon = e1["pilot_lon"] if e1["pilot_lon"] is not None else e2["pilot_lon"]
        pilot_alt = e1["pilot_alt_m"] if e1["pilot_alt_m"] is not None else e2["pilot_alt_m"]
        area_ceil = e1["area_ceil_m"] if e1["area_ceil_m"] is not None else e2["area_ceil_m"]
        area_floor = e1["area_floor_m"] if e1["area_floor_m"] is not None else e2["area_floor_m"]
        node_id = e1["node_id"] or e2["node_id"]
        is_active = max(e1["is_active"] or 0, e2["is_active"] or 0)

        # Prefer non-1970 encounter_id as primary target
        final_id = t_id
        if any(t_id.startswith(p) for p in ["ENC-1970", "ENC-1972", "ENC-1995", "ENC-2060", "ENC-2061"]):
            if not any(s_id.startswith(p) for p in ["ENC-1970", "ENC-1972", "ENC-1995", "ENC-2060", "ENC-2061"]):
                final_id = s_id

        primary_mac = e1["mac"] if (e1["mac"] and e1["mac"] != "UNKNOWN") else e2["mac"]

        # Delete existing split rows to prevent PRIMARY KEY uniqueness conflicts
        conn.execute("DELETE FROM encounters WHERE encounter_id IN (?, ?);", (t_id, s_id))

        conn.execute("""
            INSERT OR REPLACE INTO encounters (
                encounter_id, mac, serial_number, first_seen, first_seen_iso,
                last_seen, last_seen_iso, duration_s, packet_count, transports,
                channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps,
                max_rate_mbps, phy_rate_dist_json, min_rssi_dbm, max_rssi_dbm, avg_rssi_dbm,
                min_alt_m, max_alt_m, min_height_m, max_height_m, min_pressure_alt_m,
                max_pressure_alt_m, max_speed_mps, pilot_lat, pilot_lon, pilot_alt_m,
                area_ceil_m, area_floor_m, operator_id, self_id_desc, drone_make,
                drone_model, node_id, trajectory_json, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            final_id, primary_mac, serial, f_seen, f_iso, l_seen, l_iso, dur, pkts, transports,
            channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps, max_rate_mbps,
            phy_rate_dist_json, min_rssi, max_rssi, avg_rssi, min_alt, max_alt, min_h, max_h, min_p,
            max_p, max_spd, pilot_lat, pilot_lon, pilot_alt, area_ceil, area_floor, operator_id,
            self_id_desc, drone_make, drone_model, node_id, traj_json, is_active
        ))
        conn.commit()
        merged_total += 1

    return merged_total


def get_db_connection(db_path: str, timeout_s: float = 300.0) -> sqlite3.Connection:
    """
    Returns a SQLite connection configured with Row factory, WAL mode,
    canonical schema, non-destructive migrations, and automated backfill applied.
    """
    conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
