#!/usr/bin/env python3
"""
Tactical Drone Remote ID Web Dashboard - FastAPI Backend Server
Provides REST APIs, real-time WebSockets, and static UI file serving
for airspace monitoring, live radar visualization, and deep packet inspection.
"""

import asyncio
import base64
import csv
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Add repo root and scanner dir to sys.path so modules can be imported
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

scanner_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if scanner_dir not in sys.path:
    sys.path.insert(0, scanner_dir)

try:
    from scanner.db import get_db_connection as db_get_connection, reconcile_stale_encounters
    from scanner.drone_models import infer_drone_model
except ImportError:
    from db import get_db_connection as db_get_connection, reconcile_stale_encounters
    from drone_models import infer_drone_model

try:
    from scanner.scanner_config import (
        load_scanner_config,
        save_scanner_config,
        get_default_config_path,
    )
except ImportError:
    from scanner_config import (
        load_scanner_config,
        save_scanner_config,
        get_default_config_path,
    )

app = FastAPI(
    title="Tactical Drone Remote ID Airspace Monitor",
    description="Real-time ASTM F3411 Drone Remote ID monitoring, radar mapping, and telemetry analysis API",
    version="2.0.0"
)

# Enable CORS for local development and embedded clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database and Log Paths (Configurable dynamically via environment or run_dashboard.py)
def get_db_path() -> str:
    return os.environ.get("RID_DB_PATH", "rid_detections.db")


def get_jsonl_path() -> str:
    return os.environ.get("RID_JSONL_PATH", "rid_packets.jsonl")


def get_timeout_s() -> float:
    return float(os.environ.get("RID_TIMEOUT_S", "300.0"))


def get_scanner_config_path() -> str:
    env_path = os.environ.get("RID_SCANNER_CONFIG_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return get_default_config_path()


def get_current_scanner_config() -> Dict[str, Any]:
    return load_scanner_config(get_scanner_config_path())


def get_db_connection() -> sqlite3.Connection:
    """Returns a SQLite connection configured for concurrent non-blocking WAL reads."""
    db_file = get_db_path()
    timeout_s = get_timeout_s()
    return db_get_connection(db_file, timeout_s=timeout_s)


# ============================================================================
# REST Endpoints: Statistics, Config & Encounters Feed
# ============================================================================

@app.get("/api/config/scanner")
def get_scanner_config_endpoint():
    """Returns the scanner station parameters, coordinates, and range rings from disk."""
    return get_current_scanner_config()


@app.post("/api/config/scanner")
def update_scanner_config_endpoint(payload: Dict[str, Any]):
    """Updates and persists scanner station parameters directly to the JSON file on disk."""
    config_path = get_scanner_config_path()
    current = load_scanner_config(config_path)

    # Check if scanner configuration is locked in file on disk
    if current.get("locked", False):
        raise HTTPException(
            status_code=403,
            detail="Scanner node location is locked in configuration file on disk ('locked': true). Edit scanner_config.json directly on disk to change or unlock position."
        )

    if "latitude" in payload and payload["latitude"] is not None:
        current["latitude"] = float(payload["latitude"])
    elif "lat" in payload and payload["lat"] is not None:
        current["latitude"] = float(payload["lat"])

    if "longitude" in payload and payload["longitude"] is not None:
        current["longitude"] = float(payload["longitude"])
    elif "lon" in payload and payload["lon"] is not None:
        current["longitude"] = float(payload["lon"])

    if "altitude_m" in payload and payload["altitude_m"] is not None:
        current["altitude_m"] = float(payload["altitude_m"])
    elif "alt_m" in payload and payload["alt_m"] is not None:
        current["altitude_m"] = float(payload["alt_m"])

    if "name" in payload and payload["name"]:
        current["name"] = str(payload["name"])

    if "range_rings_m" in payload and isinstance(payload["range_rings_m"], list):
        current["range_rings_m"] = [float(r) for r in payload["range_rings_m"]]

    if "show_range_rings" in payload:
        current["show_range_rings"] = bool(payload["show_range_rings"])

    if "enabled" in payload:
        current["enabled"] = bool(payload["enabled"])

    if "locked" in payload:
        current["locked"] = bool(payload["locked"])

    saved = save_scanner_config(current, config_path)
    return {"status": "ok", "scanner": saved}


@app.get("/api/stats")
def get_stats():
    """Returns global airspace metrics, scanner station parameters, and transport breakdowns."""
    timeout_s = get_timeout_s()
    conn = get_db_connection()
    reconcile_stale_encounters(conn, timeout_s)

    total_enc = conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0]
    active_enc = conn.execute("SELECT COUNT(*) FROM encounters WHERE is_active = 1").fetchone()[0]
    total_pkts = conn.execute("SELECT SUM(packet_count) FROM encounters").fetchone()[0] or 0
    unique_macs = conn.execute("SELECT COUNT(DISTINCT mac) FROM encounters").fetchone()[0]
    unique_serials = conn.execute("SELECT COUNT(DISTINCT serial_number) FROM encounters WHERE serial_number IS NOT NULL").fetchone()[0]
    unique_ops = conn.execute("SELECT COUNT(DISTINCT operator_id) FROM encounters WHERE operator_id IS NOT NULL").fetchone()[0]

    min_time_iso = conn.execute("SELECT MIN(first_seen_iso) FROM encounters").fetchone()[0]
    max_time_iso = conn.execute("SELECT MAX(last_seen_iso) FROM encounters").fetchone()[0]

    # Transport breakdown count
    transports_map = {"bt4": 0, "bt5": 0, "wifi": 0, "nan": 0}
    rows = conn.execute("SELECT transports, packet_count FROM encounters").fetchall()
    for r in rows:
        t_str = r["transports"] or ""
        pkts = r["packet_count"] or 1
        for t in t_str.split(","):
            t = t.strip().lower()
            if t in transports_map:
                transports_map[t] += pkts

    scanner_config = get_current_scanner_config()

    return {
        "total_encounters": total_enc,
        "active_encounters": active_enc,
        "closed_encounters": max(0, total_enc - active_enc),
        "total_packets": total_pkts,
        "unique_macs": unique_macs,
        "unique_serials": unique_serials,
        "unique_operators": unique_ops,
        "first_seen_iso": min_time_iso,
        "last_seen_iso": max_time_iso,
        "transports_breakdown": transports_map,
        "scanner": scanner_config,
        "server_time_iso": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/encounters")
def get_encounters(
    active_only: bool = False,
    search: Optional[str] = None,
    mac: Optional[str] = None,
    serial: Optional[str] = None,
    operator: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    """Returns a list of flight encounters with summary telemetry for the left-hand feed."""
    timeout_s = get_timeout_s()
    conn = get_db_connection()
    reconcile_stale_encounters(conn, timeout_s)

    query = "SELECT * FROM encounters WHERE 1=1"
    params = []

    if active_only:
        query += " AND is_active = 1"
    if search:
        query += " AND (mac LIKE ? OR serial_number LIKE ? OR operator_id LIKE ? OR encounter_id LIKE ?)"
        s = f"%{search}%"
        params.extend([s, s, s, s])
    if mac:
        query += " AND mac LIKE ?"
        params.append(f"%{mac}%")
    if serial:
        query += " AND serial_number LIKE ?"
        params.append(f"%{serial}%")
    if operator:
        query += " AND operator_id LIKE ?"
        params.append(f"%{operator}%")
    if since:
        try:
            dt = datetime.fromisoformat(since).timestamp()
            query += " AND first_seen >= ?"
            params.append(dt)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ISO format for 'since'")

    query += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    now = time.time()

    encounters = []
    for r in rows:
        is_active = bool(r["is_active"] and (now - r["last_seen"] <= timeout_s))
        
        # Extract latest point from trajectory for real-time map marker positioning
        traj = json.loads(r["trajectory_json"]) if r["trajectory_json"] else []
        latest_point = traj[-1] if traj else None # [lat, lon, alt, speed, heading, ts]

        d_make = r["drone_make"] if "drone_make" in r.keys() else None
        d_model = r["drone_model"] if "drone_model" in r.keys() else None
        s_num = r["serial_number"]
        drone_info = infer_drone_model(s_num) if s_num else {}
        if not d_make and drone_info.get("make"):
            d_make = drone_info.get("make")
        if not d_model and drone_info.get("model"):
            d_model = drone_info.get("model")
        if d_make:
            drone_info["make"] = d_make
        if d_model:
            drone_info["model"] = d_model

        encounters.append({
            "encounter_id": r["encounter_id"],
            "mac": r["mac"],
            "serial_number": r["serial_number"],
            "drone_make": d_make,
            "drone_model": d_model,
            "drone_info": drone_info,
            "operator_id": r["operator_id"],
            "self_id_desc": r["self_id_desc"],
            "first_seen": r["first_seen"],
            "first_seen_iso": r["first_seen_iso"],
            "last_seen": r["last_seen"],
            "last_seen_iso": r["last_seen_iso"],
            "duration_s": r["duration_s"],
            "packet_count": r["packet_count"],
            "transports": r["transports"].split(",") if r["transports"] else [],
            "channels": r["channels"].split(",") if r["channels"] else [],
            "min_rssi_dbm": r["min_rssi_dbm"],
            "max_rssi_dbm": r["max_rssi_dbm"],
            "avg_rssi_dbm": r["avg_rssi_dbm"],
            "min_alt_m": r["min_alt_m"],
            "max_alt_m": r["max_alt_m"],
            "min_height_m": r["min_height_m"] if "min_height_m" in r.keys() else None,
            "max_height_m": r["max_height_m"] if "max_height_m" in r.keys() else None,
            "min_pressure_alt_m": r["min_pressure_alt_m"] if "min_pressure_alt_m" in r.keys() else None,
            "max_pressure_alt_m": r["max_pressure_alt_m"] if "max_pressure_alt_m" in r.keys() else None,
            "max_speed_mps": r["max_speed_mps"],
            "pilot_lat": r["pilot_lat"],
            "pilot_lon": r["pilot_lon"],
            "pilot_alt_m": r["pilot_alt_m"],
            "area_ceil_m": r["area_ceil_m"] if "area_ceil_m" in r.keys() else None,
            "area_floor_m": r["area_floor_m"] if "area_floor_m" in r.keys() else None,
            "is_active": is_active,
            "latest_position": {
                "lat": latest_point[0],
                "lon": latest_point[1],
                "alt_m": latest_point[2],
                "speed_mps": latest_point[3],
                "heading_deg": latest_point[4],
                "timestamp": latest_point[5],
                "height_m": latest_point[6] if len(latest_point) > 6 else None,
                "height_type": latest_point[7] if len(latest_point) > 7 else None,
                "pressure_alt_m": latest_point[8] if len(latest_point) > 8 else None,
                "vertical_speed_mps": latest_point[9] if len(latest_point) > 9 else None,
            } if latest_point else None,
            "trajectory": traj,
            "trajectory_point_count": len(traj),
        })

    return {"encounters": encounters, "count": len(encounters)}


@app.get("/api/encounters/{encounter_id}")
def get_encounter_details(encounter_id: str):
    """Returns detailed flight inspection data including the complete trajectory array."""
    timeout_s = get_timeout_s()
    conn = get_db_connection()
    reconcile_stale_encounters(conn, timeout_s)

    row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (encounter_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Encounter '{encounter_id}' not found")

    traj = json.loads(row["trajectory_json"]) if row["trajectory_json"] else []
    # traj: list of [lat, lon, alt, speed, heading, ts]

    now = time.time()
    is_active = bool(row["is_active"] and (now - row["last_seen"] <= timeout_s))

    d_make = row["drone_make"] if "drone_make" in row.keys() else None
    d_model = row["drone_model"] if "drone_model" in row.keys() else None
    s_num = row["serial_number"]
    drone_info = infer_drone_model(s_num) if s_num else {}
    if not d_make and drone_info.get("make"):
        d_make = drone_info.get("make")
    if not d_model and drone_info.get("model"):
        d_model = drone_info.get("model")
    if d_make:
        drone_info["make"] = d_make
    if d_model:
        drone_info["model"] = d_model

    return {
        "encounter_id": row["encounter_id"],
        "mac": row["mac"],
        "serial_number": row["serial_number"],
        "drone_make": d_make,
        "drone_model": d_model,
        "drone_info": drone_info,
        "operator_id": row["operator_id"],
        "self_id_desc": row["self_id_desc"],
        "first_seen": row["first_seen"],
        "first_seen_iso": row["first_seen_iso"],
        "last_seen": row["last_seen"],
        "last_seen_iso": row["last_seen_iso"],
        "duration_s": row["duration_s"],
        "packet_count": row["packet_count"],
        "transports": row["transports"].split(",") if row["transports"] else [],
        "channels": row["channels"].split(",") if row["channels"] else [],
        "min_rssi_dbm": row["min_rssi_dbm"],
        "max_rssi_dbm": row["max_rssi_dbm"],
        "avg_rssi_dbm": row["avg_rssi_dbm"],
        "min_alt_m": row["min_alt_m"],
        "max_alt_m": row["max_alt_m"],
        "min_height_m": row["min_height_m"] if "min_height_m" in row.keys() else None,
        "max_height_m": row["max_height_m"] if "max_height_m" in row.keys() else None,
        "min_pressure_alt_m": row["min_pressure_alt_m"] if "min_pressure_alt_m" in row.keys() else None,
        "max_pressure_alt_m": row["max_pressure_alt_m"] if "max_pressure_alt_m" in row.keys() else None,
        "max_speed_mps": row["max_speed_mps"],
        "pilot_lat": row["pilot_lat"],
        "pilot_lon": row["pilot_lon"],
        "pilot_alt_m": row["pilot_alt_m"],
        "area_ceil_m": row["area_ceil_m"] if "area_ceil_m" in row.keys() else None,
        "area_floor_m": row["area_floor_m"] if "area_floor_m" in row.keys() else None,
        "is_active": is_active,
        "trajectory": traj,
    }


# ============================================================================
# Deep Packet Inspector Endpoint
# ============================================================================

@app.get("/api/encounters/{encounter_id}/packets")
def get_encounter_packets(encounter_id: str):
    """
    Returns every individual raw and decoded ASTM packet captured during the encounter.
    Searches the JSONL replay log file if present, or reconstructs from trajectory fixes.
    """
    packets = []

    # 1. Attempt to extract from JSONL log files
    jsonl_log_path = get_jsonl_path()
    db_path = get_db_path()
    log_candidates = [
        jsonl_log_path,
        os.path.join(os.path.dirname(db_path), "rid_packets.jsonl"),
        "rid_packets.jsonl",
    ]
    # Also check daily rotated logs in directory
    log_dir = os.path.dirname(os.path.abspath(jsonl_log_path)) if jsonl_log_path else "."
    if os.path.exists(log_dir):
        for fname in sorted(os.listdir(log_dir), reverse=True):
            if fname.endswith(".jsonl") and ("rid" in fname or "capture" in fname or "replay" in fname):
                log_candidates.append(os.path.join(log_dir, fname))

    found_in_jsonl = False
    for path in log_candidates:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or not line.startswith("{"):
                            continue
                        try:
                            rec = json.loads(line)
                            if rec.get("encounter_id") == encounter_id:
                                # Decode base64 message blocks if present
                                decoded_blocks = []
                                for b64_str in rec.get("messages_b64", []):
                                    try:
                                        raw_b = base64.b64decode(b64_str)
                                        parsed = decode_astm_message(raw_b)
                                        if parsed:
                                            decoded_blocks.append(parsed)
                                    except Exception:
                                        pass

                                packets.append({
                                    "index": len(packets) + 1,
                                    "time_offset_ms": rec.get("time_offset_ms", 0),
                                    "timestamp_iso": rec.get("timestamp_iso"),
                                    "transport": rec.get("transport"),
                                    "channel": rec.get("channel"),
                                    "rssi_dbm": rec.get("rssi_dbm"),
                                    "mac": rec.get("mac"),
                                    "serial": rec.get("serial"),
                                    "counter": rec.get("counter", 0),
                                    "messages_b64": rec.get("messages_b64", []),
                                    "decoded_messages": decoded_blocks,
                                })
                                found_in_jsonl = True
                        except Exception:
                            continue
                if found_in_jsonl:
                    break
            except Exception:
                pass

    # 2. If no JSONL log found, synthesize packet records from SQLite trajectory points
    if not packets:
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (encounter_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Encounter '{encounter_id}' not found")
        
        traj = json.loads(row["trajectory_json"]) if row["trajectory_json"] else []
        t0 = row["first_seen"]
        for idx, pt in enumerate(traj):
            # pt: [lat, lon, alt, speed, heading, ts]
            ts = pt[5] if len(pt) > 5 else t0
            delta_ms = int((ts - t0) * 1000)
            packets.append({
                "index": idx + 1,
                "time_offset_ms": delta_ms,
                "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "transport": row["transports"].split(",")[0] if row["transports"] else "unknown",
                "channel": row["channels"].split(",")[0] if row["channels"] else "N/A",
                "rssi_dbm": row["avg_rssi_dbm"],
                "mac": row["mac"],
                "serial": row["serial_number"],
                "counter": idx,
                "messages_b64": [],
                "decoded_messages": [
                    {
                        "type": "Basic ID",
                        "id": row["serial_number"],
                        "id_type_name": "Serial Number (ANSI/CTA-2063-A)"
                    } if row["serial_number"] else None,
                    {
                        "type": "Location",
                        "lat": pt[0],
                        "lon": pt[1],
                        "alt": pt[2],
                        "geodetic_altitude_m": pt[2],
                        "speed_mps": pt[3],
                        "direction_deg": pt[4],
                        "height_m": pt[6] if len(pt) > 6 else None,
                        "height_type": pt[7] if len(pt) > 7 else None,
                        "pressure_altitude_m": pt[8] if len(pt) > 8 else None,
                        "vertical_speed_mps": pt[9] if len(pt) > 9 else None,
                    },
                    {
                        "type": "Operator ID",
                        "operator_id": row["operator_id"],
                    } if row["operator_id"] else None
                ],
            })
            # Filter out None blocks
            packets[-1]["decoded_messages"] = [m for m in packets[-1]["decoded_messages"] if m]

    return {
        "encounter_id": encounter_id,
        "packet_count": len(packets),
        "packets": packets,
    }


# ============================================================================
# File Export Endpoints (GeoJSON, CSV, JSONL)
# ============================================================================

@app.get("/api/export/{encounter_id}/geojson")
def export_geojson(encounter_id: str):
    """Exports flight trajectory as an RFC 7946 compliant GeoJSON FeatureCollection."""
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (encounter_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Encounter not found")

    traj = json.loads(row["trajectory_json"]) if row["trajectory_json"] else []
    coordinates = [[pt[1], pt[0], pt[2] or 0.0] for pt in traj] # [lon, lat, alt]

    features = []

    # 1. LineString feature for complete flight trajectory
    if len(coordinates) >= 2:
        features.append({
            "type": "Feature",
            "properties": {
                "name": f"Flight Track {encounter_id}",
                "mac": row["mac"],
                "serial_number": row["serial_number"],
                "drone_make": row["drone_make"] if "drone_make" in row.keys() else None,
                "drone_model": row["drone_model"] if "drone_model" in row.keys() else None,
                "operator_id": row["operator_id"],
                "duration_s": row["duration_s"],
                "max_altitude_m": row["max_alt_m"],
                "max_speed_mps": row["max_speed_mps"],
                "transports": row["transports"],
            },
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates
            }
        })

    # 2. Point features for every discrete recorded packet fix
    for idx, pt in enumerate(traj):
        features.append({
            "type": "Feature",
            "properties": {
                "point_index": idx + 1,
                "timestamp_epoch": pt[5] if len(pt) > 5 else None,
                "altitude_m": pt[2],
                "speed_mps": pt[3],
                "heading_deg": pt[4],
            },
            "geometry": {
                "type": "Point",
                "coordinates": [pt[1], pt[0], pt[2] or 0.0]
            }
        })

    # 3. Pilot / Home Point feature if present
    if row["pilot_lat"] is not None and row["pilot_lon"] is not None:
        features.append({
            "type": "Feature",
            "properties": {
                "name": "Pilot / GCS Home Location",
                "altitude_m": row["pilot_alt_m"],
            },
            "geometry": {
                "type": "Point",
                "coordinates": [row["pilot_lon"], row["pilot_lat"], row["pilot_alt_m"] or 0.0]
            }
        })

    geojson_doc = {
        "type": "FeatureCollection",
        "properties": {
            "encounter_id": encounter_id,
            "mac": row["mac"],
            "serial_number": row["serial_number"],
            "operator_id": row["operator_id"],
            "generated_iso": datetime.now(timezone.utc).isoformat(),
        },
        "features": features
    }

    return Response(
        content=json.dumps(geojson_doc, indent=2),
        media_type="application/geo+json",
        headers={"Content-Disposition": f"attachment; filename={encounter_id}.geojson"}
    )


@app.get("/api/export/{encounter_id}/csv")
def export_csv(encounter_id: str):
    """Exports flight trajectory coordinates and telemetry points as tabular CSV."""
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (encounter_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Encounter not found")

    traj = json.loads(row["trajectory_json"]) if row["trajectory_json"] else []
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "index", "timestamp_epoch", "latitude", "longitude",
        "geodetic_altitude_m", "pressure_altitude_m", "height_m", "height_type",
        "vertical_speed_mps", "speed_mps", "heading_deg"
    ])

    for idx, pt in enumerate(traj):
        ts = pt[5] if len(pt) > 5 else ""
        h_m = pt[6] if len(pt) > 6 and pt[6] is not None else ""
        h_type = pt[7] if len(pt) > 7 and pt[7] is not None else ""
        p_alt = pt[8] if len(pt) > 8 and pt[8] is not None else ""
        v_spd = pt[9] if len(pt) > 9 and pt[9] is not None else ""
        writer.writerow([idx + 1, ts, pt[0], pt[1], pt[2], p_alt, h_m, h_type, v_spd, pt[3], pt[4]])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={encounter_id}.csv"}
    )


# ============================================================================
# WebSocket: Real-Time Airspace Telemetry Stream
# ============================================================================

@app.websocket("/ws/live")
async def websocket_live_stream(websocket: WebSocket):
    """
    Pushes live active drone positions, new packet bursts, and stats every 1.5s
    to connected tactical map clients.
    """
    await websocket.accept()
    try:
        while True:
            timeout_s = get_timeout_s()
            conn = get_db_connection()
            reconcile_stale_encounters(conn, timeout_s)

            # Query active flights
            rows = conn.execute("""
                SELECT * FROM encounters 
                WHERE is_active = 1 
                ORDER BY last_seen DESC LIMIT 20
            """).fetchall()

            now = time.time()
            active_drones = []
            for r in rows:
                traj = json.loads(r["trajectory_json"]) if r["trajectory_json"] else []
                latest = traj[-1] if traj else None
                d_make = r["drone_make"] if "drone_make" in r.keys() else None
                d_model = r["drone_model"] if "drone_model" in r.keys() else None
                if not d_make and r["serial_number"]:
                    inf = infer_drone_model(r["serial_number"])
                    if inf.get("is_inferred"):
                        d_make = inf.get("make")
                        d_model = inf.get("model")

                active_drones.append({
                    "encounter_id": r["encounter_id"],
                    "mac": r["mac"],
                    "serial_number": r["serial_number"],
                    "drone_make": d_make,
                    "drone_model": d_model,
                    "operator_id": r["operator_id"],
                    "duration_s": r["duration_s"],
                    "packet_count": r["packet_count"],
                    "transports": r["transports"].split(",") if r["transports"] else [],
                    "latest_position": {
                        "lat": latest[0],
                        "lon": latest[1],
                        "alt_m": latest[2],
                        "speed_mps": latest[3],
                        "heading_deg": latest[4],
                        "timestamp": latest[5],
                    } if latest else None,
                    "max_alt_m": r["max_alt_m"],
                    "avg_rssi_dbm": r["avg_rssi_dbm"],
                    "last_seen_iso": r["last_seen_iso"],
                })

            payload = {
                "type": "live_telemetry",
                "timestamp": now,
                "timestamp_iso": datetime.now(timezone.utc).isoformat(),
                "active_count": len(active_drones),
                "active_drones": active_drones,
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(1.5)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ============================================================================
# FAA Declaration of Compliance (DOC) Registry Lookup
# ============================================================================

FAA_DOC_CACHE: Dict[str, Dict[str, Any]] = {}

@app.get("/api/faa_lookup")
def query_faa_doc_registry(serial: str = Query(..., description="Drone Serial Number / UAS ID to look up")):
    """
    Queries the official FAA Declaration of Compliance (DOC) Remote ID Registry
    (https://uasdoc.faa.gov/api/v1/serialNumbers) to look up drone manufacturer,
    model name, series, compliance tracking number, and approval status.
    Implements in-memory caching and updates local encounters DB with verified registration.
    """
    clean_serial = serial.strip()
    if not clean_serial:
        raise HTTPException(status_code=400, detail="Serial number is required")

    cache_key = clean_serial.upper()
    if cache_key in FAA_DOC_CACHE:
        return FAA_DOC_CACHE[cache_key]

    endpoint = "https://uasdoc.faa.gov/api/v1/serialNumbers"
    params = urllib.parse.urlencode({
        "itemsPerPage": 8,
        "pageIndex": 0,
        "orderBy[0]": "updatedAt",
        "orderBy[1]": "DESC",
        "findBy": "serialNumber",
        "serialNumber": clean_serial
    })
    req_url = f"{endpoint}?{params}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://uasdoc.faa.gov/listdocs",
        "client": "external"
    }

    try:
        req = urllib.request.Request(req_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            if response.status != 200:
                result = {
                    "status": "error",
                    "found": False,
                    "serial": clean_serial,
                    "message": f"FAA server returned HTTP {response.status}"
                }
                return result

            raw_body = response.read().decode("utf-8")
            data = json.loads(raw_body)
            items = data.get("data", {}).get("items", [])

            if items:
                doc = items[0]
                faa_make = doc.get("makeName")
                faa_model = doc.get("modelName")
                faa_series = doc.get("series")
                full_model = f"{faa_model} ({faa_series})" if faa_series and faa_series != faa_model else (faa_model or faa_series)

                # Persist verified official make & model to encounters SQLite database
                try:
                    db_conn = get_db_connection()
                    db_conn.execute(
                        "UPDATE encounters SET drone_make = ?, drone_model = ? WHERE serial_number = ? OR serial_number LIKE ?",
                        (faa_make, full_model or faa_model, clean_serial, f"%{clean_serial}%")
                    )
                    db_conn.commit()
                except Exception:
                    pass

                result = {
                    "status": "ok",
                    "found": True,
                    "serial": clean_serial,
                    "make": faa_make,
                    "model": faa_model,
                    "series": faa_series,
                    "tracking_number": doc.get("trackingNumber"),
                    "doc_status": doc.get("status"),
                    "category": doc.get("category"),
                    "applicant": doc.get("applicantName"),
                    "created_at": doc.get("createdAt"),
                    "updated_at": doc.get("updatedAt"),
                    "total_matches": len(items),
                }
            else:
                result = {
                    "status": "ok",
                    "found": False,
                    "serial": clean_serial,
                    "message": "No matching Declaration of Compliance found in FAA registry for this serial number."
                }

            FAA_DOC_CACHE[cache_key] = result
            return result

    except urllib.error.HTTPError as e:
        return {
            "status": "error",
            "found": False,
            "serial": clean_serial,
            "message": f"FAA registry service error (HTTP {e.code})"
        }
    except Exception as e:
        return {
            "status": "error",
            "found": False,
            "serial": clean_serial,
            "message": f"Could not reach FAA DOC registry ({str(e)})"
        }


# ============================================================================
# Static UI Assets Mounting
# ============================================================================

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
