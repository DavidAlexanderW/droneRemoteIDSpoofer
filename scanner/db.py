#!/usr/bin/env python3
"""
Centralized SQLite Database Manager & Schema Migration Engine for Drone Remote ID Encounters.
Ensures a single canonical source of truth for the SQLite schema, non-destructive column migrations,
and automated CTA-2063-A make/model backfilling.
"""

import os
import sqlite3
import sys
import time
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
        unfilled = conn.execute("SELECT encounter_id, serial_number FROM encounters WHERE drone_make IS NULL AND serial_number IS NOT NULL;").fetchall()
        for r in unfilled:
            # Supports both sqlite3.Row and tuple
            enc_id = r[0]
            s_num = r[1]
            inf = infer_drone_model(s_num)
            if inf.get("is_inferred"):
                conn.execute("UPDATE encounters SET drone_make = ?, drone_model = ? WHERE encounter_id = ?;", (inf["make"], inf["model"], enc_id))
    except Exception:
        pass

    # 4. Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_mac ON encounters(mac);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_serial ON encounters(serial_number);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_time ON encounters(first_seen, last_seen);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_active ON encounters(is_active);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_encounters_node ON encounters(node_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_status ON receiver_nodes(status);")

    # 5. Auto-reconcile lingering active encounters from prior runs
    now = time.time()
    conn.execute("UPDATE encounters SET is_active = 0 WHERE is_active = 1 AND (? - last_seen) > ?;",
                 (now, timeout_s))
    conn.commit()


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
