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
    conn = sqlite3.connect(db_path, timeout=10.0)
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
