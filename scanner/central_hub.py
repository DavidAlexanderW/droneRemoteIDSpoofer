#!/usr/bin/env python3
"""
Tactical Drone Remote ID - Central Ingestion Hub & Sensor Fusion Server
Headless service receiving multi-node telemetry streams over WebSockets.
Requires zero radio hardware and runs as an unprivileged service in cloud VMs or containers.

Features:
- WebSocket `/stream/node`: Handshake, sync state watermark tracking, batch catch-up & real-time streaming
- Sensor Fusion & Spatial Deduplication across multiple receiver stations
- Dual-Tier Central Storage:
  - Hot tier: `rid_detections_central.db` SQLite (WAL mode)
  - Cold tier: Daily forensic replay JSONL files in `central_logs/rid_packets_YYYYMMDD.jsonl`
- REST APIs: `/api/nodes`, `/api/health`
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Ensure repo root and scanner dir in sys.path
scanner_dir = os.path.abspath(os.path.dirname(__file__))
repo_root = os.path.abspath(os.path.join(scanner_dir, ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
if scanner_dir not in sys.path:
    sys.path.insert(0, scanner_dir)

try:
    from scanner.db import (
        get_db_connection,
        get_node_sync_watermark,
        get_receiver_nodes,
        init_encounters_db,
        touch_receiver_node_heartbeat,
        update_node_sync_watermark,
        upsert_receiver_node,
        update_receiver_node_position,
    )
    from scanner.drone_models import infer_drone_model
except ImportError:
    from db import (
        get_db_connection,
        get_node_sync_watermark,
        get_receiver_nodes,
        init_encounters_db,
        touch_receiver_node_heartbeat,
        update_node_sync_watermark,
        upsert_receiver_node,
        update_receiver_node_position,
    )
    from drone_models import infer_drone_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("DroneRIDCentralHub")


# ============================================================================
# Central Daily Forensic Replay Logger (Cold Tier)
# ============================================================================

class CentralDailyReplayLogger:
    """Manages appending validated raw frames and envelopes to central daily JSONL files."""

    def __init__(self, log_dir: str = "central_logs"):
        self.log_dir = os.path.abspath(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)
        self.current_day_str: Optional[str] = None
        self.file_handle = None
        self.lock = asyncio.Lock()

    def _get_file_handle(self, epoch_ts: float):
        day_str = datetime.fromtimestamp(epoch_ts, timezone.utc).strftime("%Y%m%d")
        if day_str != self.current_day_str or self.file_handle is None:
            if self.file_handle:
                try:
                    self.file_handle.close()
                except Exception:
                    pass
            self.current_day_str = day_str
            target_path = os.path.join(self.log_dir, f"rid_packets_{day_str}.jsonl")
            self.file_handle = open(target_path, "a", encoding="utf-8")
            logger.info(f"[*] Appending central forensic replay log to: {target_path}")
        return self.file_handle

    async def log_packet(self, packet_envelope: Dict[str, Any], encounter_id: Optional[str] = None):
        """Asynchronously writes a standardized packet record to the central daily JSONL."""
        now_ts = time.time()
        min_valid = 1700000000.0
        max_valid = 2000000000.0  # May 18, 2033 UTC (catches uncalibrated hardware tick overflows like 2061)

        ts = packet_envelope.get("reception_timestamp") or packet_envelope.get("timestamp") or packet_envelope.get("timestamp_epoch")
        if ts is None and packet_envelope.get("timestamp_iso"):
            try:
                dt = datetime.fromisoformat(packet_envelope["timestamp_iso"].replace("Z", "+00:00"))
                ts = dt.timestamp()
            except Exception:
                ts = now_ts
        if ts is None or ts < min_valid or ts > max_valid:
            ts = now_ts

        async with self.lock:
            handle = self._get_file_handle(ts)
            record = {
                "timestamp": ts,
                "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "node_id": packet_envelope.get("node_id", "unknown"),
                "node_meta": packet_envelope.get("node_meta", {}),
                "transport": packet_envelope.get("transport", "unknown"),
                "channel": packet_envelope.get("channel"),
                "rssi_dbm": packet_envelope.get("rssi_dbm"),
                "mac": packet_envelope.get("mac", "UNKNOWN"),
                "serial": packet_envelope.get("serial_number") or packet_envelope.get("serial"),
                "counter": packet_envelope.get("counter", 0),
                "messages_b64": packet_envelope.get("messages_b64", []),
                "messages": packet_envelope.get("messages", []),
                "rate_mbps": packet_envelope.get("rate_mbps"),
                "modulation": packet_envelope.get("modulation"),
                "encounter_id": encounter_id,
            }
            handle.write(json.dumps(record) + "\n")
            handle.flush()


# ============================================================================
# Central Spatial Deduplicator & Multi-Receiver Fusion
# ============================================================================

class MultiNodeDeduplicator:
    """
    Deduplicates simultaneous RF reception of the same frame across multiple sensor nodes,
    and constructs the multi-node RSSI sighting profile.
    """

    def __init__(self, ttl_seconds: float = 10.0):
        self.ttl = ttl_seconds
        # Mapping: dedup_key -> {"first_seen": ts, "node_rssi": {node_id: rssi}, "primary_node": node_id}
        self.seen_packets: Dict[Tuple[str, str, float, int], Dict[str, Any]] = {}
        self.last_cleanup = time.time()

    def process(self, envelope: Dict[str, Any]) -> Tuple[bool, Dict[str, int], str]:
        """
        Evaluates an incoming envelope for multi-node duplication.
        Returns (is_new_packet, node_rssi_map, primary_node_id).
        """
        now = time.time()
        if now - self.last_cleanup > 5.0:
            self._cleanup(now)

        mac = envelope.get("mac", "UNKNOWN")
        serial = envelope.get("serial_number") or envelope.get("serial")
        entity_id = serial or mac
        ts = envelope.get("timestamp") or envelope.get("timestamp_epoch")
        if ts is None and envelope.get("timestamp_iso"):
            try:
                dt = datetime.fromisoformat(envelope["timestamp_iso"].replace("Z", "+00:00"))
                ts = dt.timestamp()
            except Exception:
                ts = now
        if ts is None:
            ts = now
        counter = envelope.get("counter", 0)
        transport = envelope.get("transport", "")
        node_id = envelope.get("node_id", "default_node")
        rssi = envelope.get("rssi_dbm")

        # Quantize timestamp to 0.5s for loose temporal grouping
        rounded_ts = round(ts * 2.0) / 2.0
        dedup_key = (entity_id, transport, rounded_ts, counter)

        if dedup_key in self.seen_packets:
            entry = self.seen_packets[dedup_key]
            if rssi is not None:
                entry["node_rssi"][node_id] = rssi
            # If this node has a stronger signal, promote it to primary
            if rssi is not None and (entry.get("max_rssi") is None or rssi > entry["max_rssi"]):
                entry["max_rssi"] = rssi
                entry["primary_node"] = node_id
            return False, entry["node_rssi"], entry["primary_node"]

        # New unique packet
        node_rssi = {node_id: rssi} if rssi is not None else {}
        self.seen_packets[dedup_key] = {
            "first_seen": now,
            "node_rssi": node_rssi,
            "primary_node": node_id,
            "max_rssi": rssi,
        }
        return True, node_rssi, node_id

    def _cleanup(self, now: float):
        expired = [k for k, v in self.seen_packets.items() if now - v["first_seen"] > self.ttl]
        for k in expired:
            del self.seen_packets[k]
        self.last_cleanup = now


# ============================================================================
# Central Ingestion Hub Application Class
# ============================================================================

class CentralIngestionHub:
    """Manages the central database connection, encounter tracker, and node streams."""

    def __init__(
        self,
        db_path: str = "rid_detections_central.db",
        log_dir: str = "central_logs",
        timeout_s: float = 300.0,
    ):
        self.db_path = os.path.abspath(db_path)
        self.log_dir = os.path.abspath(log_dir)
        self.timeout_s = timeout_s

        # Initialize database schema
        self.db_conn = get_db_connection(self.db_path, timeout_s=self.timeout_s)

        from scanner.combined_rid_listener import EncounterTracker
        self.encounter_tracker = EncounterTracker(
            db_path=self.db_path,
            timeout_s=self.timeout_s,
            persist_interval_s=1.0,
        )

        self.replay_logger = CentralDailyReplayLogger(log_dir=self.log_dir)
        self.deduplicator = MultiNodeDeduplicator()

        self.active_node_connections: Dict[str, Any] = {}
        self.pending_command_futures: Dict[str, asyncio.Future] = {}
        self.in_flight_position_updates: Dict[str, Dict[str, Any]] = {}

        self.stats = {
            "start_time": time.time(),
            "total_packets_received": 0,
            "total_batches_received": 0,
            "active_nodes": {},
        }
        self.lock = asyncio.Lock()

    def get_last_synced_epoch(self, node_id: str) -> float:
        return get_node_sync_watermark(self.db_conn, node_id)

    def register_node(self, node_id: str, node_meta: Dict[str, Any]):
        """Upserts a receiver node's registration and location in the database with in-flight guard."""
        name = node_meta.get("name", node_id)
        lat = float(node_meta.get("latitude", 0.0))
        lon = float(node_meta.get("longitude", 0.0))
        alt = float(node_meta.get("altitude_m", 0.0))
        rings = json.dumps(node_meta.get("range_rings_m", [500, 1000, 2500, 5000]))
        desc = node_meta.get("description", "")
        locked = bool(node_meta.get("locked", False))

        # In-flight guard: if a position update is currently in-flight for this node, preserve target coordinates
        if node_id in self.in_flight_position_updates:
            target = self.in_flight_position_updates[node_id]
            lat = float(target["latitude"])
            lon = float(target["longitude"])
            if target.get("altitude_m") is not None:
                alt = float(target["altitude_m"])
        else:
            # If node is already registered in DB and is unlocked, check if DB already has calibrated coordinates
            existing = self.db_conn.execute("SELECT latitude, longitude, altitude_m, locked FROM receiver_nodes WHERE node_id = ?;", (node_id,)).fetchone()
            if existing and not bool(existing["locked"]) and not locked:
                db_lat = float(existing["latitude"])
                db_lon = float(existing["longitude"])
                if (db_lat != 0.0 or db_lon != 0.0) and (lat == 0.0 and lon == 0.0):
                    lat = db_lat
                    lon = db_lon
                    alt = float(existing["altitude_m"])

        upsert_receiver_node(
            self.db_conn,
            node_id=node_id,
            name=name,
            latitude=lat,
            longitude=lon,
            altitude_m=alt,
            range_rings_json=rings,
            description=desc,
            locked=locked,
            status="ONLINE",
        )

    async def send_node_command(self, node_id: str, command: Dict[str, Any], timeout_s: float = 5.0) -> Dict[str, Any]:
        """Dispatches a JSON command to a connected edge node over WebSocket and awaits acknowledgment."""
        ws = self.active_node_connections.get(node_id)
        if not ws:
            return {"status": "offline", "detail": f"Node '{node_id}' is not connected via WebSocket."}

        req_id = command.get("request_id") or f"cmd-{node_id}-{int(time.time() * 1000)}"
        command["request_id"] = req_id

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending_command_futures[req_id] = fut

        try:
            await ws.send_text(json.dumps(command))
            response = await asyncio.wait_for(fut, timeout=timeout_s)
            return response
        except asyncio.TimeoutError:
            self.pending_command_futures.pop(req_id, None)
            return {"status": "timeout", "detail": f"Node '{node_id}' did not respond within {timeout_s}s."}
        except Exception as e:
            self.pending_command_futures.pop(req_id, None)
            return {"status": "error", "detail": str(e)}

    def heartbeat_node(self, node_id: str, packets_increment: int = 0):
        """Refreshes node heartbeat timestamp in database."""
        touch_receiver_node_heartbeat(self.db_conn, node_id, packets_increment=packets_increment)

    def update_node_watermark(self, node_id: str, last_epoch: float, count: int = 0):
        update_node_sync_watermark(self.db_conn, node_id, last_epoch, packets_synced_count=count)

    async def ingest_packet(self, envelope: Dict[str, Any]) -> Optional[str]:
        """Ingests a single packet envelope through deduplication, encounter tracker, and replay log."""
        now_ts = time.time()
        min_valid = 1700000000.0
        max_valid = 2000000000.0  # May 18, 2033 UTC (catches uncalibrated hardware tick overflows like 2061)

        ts = envelope.get("reception_timestamp") or envelope.get("timestamp") or envelope.get("timestamp_epoch")
        if ts is None and envelope.get("timestamp_iso"):
            try:
                dt = datetime.fromisoformat(envelope["timestamp_iso"].replace("Z", "+00:00"))
                ts = dt.timestamp()
            except Exception:
                ts = now_ts
        if ts is None or ts < min_valid or ts > max_valid:
            ts = now_ts
        envelope["timestamp"] = ts
        envelope["reception_timestamp"] = ts
        envelope["timestamp_iso"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()

        if not envelope.get("serial_number") and envelope.get("serial"):
            envelope["serial_number"] = envelope["serial"]

        if not envelope.get("messages") and envelope.get("messages_b64"):
            try:
                import base64
                try:
                    from scanner.parser import decode_astm_message
                except ImportError:
                    try:
                        from drone_rid_spoofer.parser import decode_astm_message
                    except ImportError:
                        from parser import decode_astm_message
                raw_blocks = [base64.b64decode(b) for b in envelope["messages_b64"]]
                envelope["messages"] = [decode_astm_message(b) for b in raw_blocks if decode_astm_message(b)]
            except Exception:
                pass

        is_new, node_rssi_map, primary_node = self.deduplicator.process(envelope)

        # Attach multi-node spatial attribution
        envelope["primary_node_id"] = primary_node
        envelope["node_id"] = primary_node
        envelope["node_rssi_map"] = node_rssi_map

        # Update encounter tracker
        encounter_id = self.encounter_tracker.update_with_packet(envelope)

        # Log to cold replay file
        await self.replay_logger.log_packet(envelope, encounter_id=encounter_id)

        self.stats["total_packets_received"] += 1
        return encounter_id

    async def ingest_batch(self, batch_payload: Dict[str, Any]) -> int:
        """Ingests a batch of catch-up packets atomically."""
        node_id = batch_payload.get("node_id", "unknown")
        items = batch_payload.get("items", [])
        max_ts = 0.0

        for item in items:
            ts = item.get("timestamp", item.get("timestamp_epoch", 0.0))
            if ts > max_ts:
                max_ts = ts
            await self.ingest_packet(item)

        if max_ts > 0.0:
            self.update_node_watermark(node_id, max_ts, count=len(items))

        self.stats["total_batches_received"] += 1
        return len(items)


# ============================================================================
# FastAPI Server Factory
# ============================================================================

def create_central_hub_app(hub: CentralIngestionHub) -> FastAPI:
    app = FastAPI(
        title="Drone Remote ID Central Ingestion Hub",
        description="Centralized multi-node receiver hub and sensor fusion engine",
        version="2.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def api_health():
        uptime = time.time() - hub.stats["start_time"]
        active_encs = len(hub.encounter_tracker.active_encounters)
        return {
            "status": "healthy",
            "uptime_seconds": round(uptime, 1),
            "total_packets_received": hub.stats["total_packets_received"],
            "total_batches_received": hub.stats["total_batches_received"],
            "active_encounters": active_encs,
        }

    @app.get("/api/nodes")
    async def api_get_nodes():
        nodes = get_receiver_nodes(hub.db_conn)
        return {"nodes": nodes, "count": len(nodes)}

    @app.post("/api/nodes/{node_id}/position")
    @app.post("/api/nodes/{node_id}/location")
    async def api_update_node_position(node_id: str, payload: Dict[str, Any]):
        """
        Updates physical coordinates of an unlocked sensor node in the database,
        engages an in-flight guard, and dispatches update_location to the connected
        edge node over WebSocket to persist the new coordinates directly to disk.
        """
        lat = payload.get("latitude", payload.get("lat"))
        lon = payload.get("longitude", payload.get("lon"))
        alt = payload.get("altitude_m", payload.get("alt_m"))

        if lat is None or lon is None:
            raise HTTPException(status_code=400, detail="Missing required 'latitude' or 'longitude' in payload.")

        # Guard against in-flight race conditions (heartbeats or packets arriving during update)
        hub.in_flight_position_updates[node_id] = {
            "latitude": float(lat),
            "longitude": float(lon),
            "altitude_m": float(alt) if alt is not None else None,
            "timestamp": time.time(),
        }

        try:
            updated = update_receiver_node_position(
                hub.db_conn,
                node_id=node_id,
                latitude=float(lat),
                longitude=float(lon),
                altitude_m=float(alt) if alt is not None else None,
            )
        except KeyError as e:
            hub.in_flight_position_updates.pop(node_id, None)
            raise HTTPException(status_code=404, detail=str(e))
        except PermissionError as e:
            hub.in_flight_position_updates.pop(node_id, None)
            raise HTTPException(status_code=403, detail=str(e))

        # Check if node is connected via WebSocket
        is_connected = node_id in hub.active_node_connections
        remote_resp = None
        if is_connected:
            remote_resp = await hub.send_node_command(
                node_id=node_id,
                command={
                    "type": "update_location",
                    "node_id": node_id,
                    "latitude": float(lat),
                    "longitude": float(lon),
                    "altitude_m": float(alt) if alt is not None else updated.get("altitude_m", 0.0),
                },
                timeout_s=5.0,
            )
        else:
            hub.in_flight_position_updates.pop(node_id, None)

        return {
            "status": "ok",
            "node": updated,
            "remote_synced": bool(remote_resp and remote_resp.get("status") == "ok"),
            "remote_response": remote_resp,
            "detail": "Synchronized to remote node disk" if is_connected else "Updated in database; will sync on node reconnect."
        }

    @app.websocket("/stream/node")
    async def websocket_node_stream(websocket: WebSocket, node_id: str = Query("unknown")):
        await websocket.accept()
        logger.info(f"[+] Node connected to WebSocket stream: '{node_id}' from {websocket.client.host}")

        actual_node_id = node_id
        # Wait for handshake payload
        try:
            raw_handshake = await asyncio.wait_for(websocket.receive_text(), timeout=15.0)
            hs_data = json.loads(raw_handshake)
            actual_node_id = hs_data.get("node_id", node_id)
            node_meta = hs_data.get("node_meta", {})

            # Register connection in active registry
            hub.active_node_connections[actual_node_id] = websocket

            # Check if there is already a calibrated position in DB for this node
            db_row = hub.db_conn.execute("SELECT latitude, longitude, altitude_m, locked FROM receiver_nodes WHERE node_id = ?;", (actual_node_id,)).fetchone()
            calibrated_pos = None
            if db_row and (float(db_row["latitude"]) != 0.0 or float(db_row["longitude"]) != 0.0):
                calibrated_pos = {
                    "latitude": float(db_row["latitude"]),
                    "longitude": float(db_row["longitude"]),
                    "altitude_m": float(db_row["altitude_m"]),
                }

            # If node is unlocked and we have calibrated coordinates in DB, preserve them during registration
            if calibrated_pos and not bool(node_meta.get("locked")):
                node_meta["latitude"] = calibrated_pos["latitude"]
                node_meta["longitude"] = calibrated_pos["longitude"]
                if calibrated_pos.get("altitude_m") is not None:
                    node_meta["altitude_m"] = calibrated_pos["altitude_m"]

            # Register node in DB
            hub.register_node(actual_node_id, node_meta)
            watermark = hub.get_last_synced_epoch(actual_node_id)

            # Send handshake ACK
            ack_response = {
                "status": "ready",
                "node_id": actual_node_id,
                "last_synced_epoch": watermark,
                "ack_enabled": True,
                "calibrated_position": calibrated_pos,
            }
            await websocket.send_text(json.dumps(ack_response))

            # Main ingestion loop
            while True:
                msg_text = await websocket.receive_text()
                data = json.loads(msg_text)
                msg_type = data.get("type", "packet")

                if msg_type == "heartbeat":
                    hb_node_id = data.get("node_id", actual_node_id)
                    # Routine heartbeat only refreshes liveness and packet count, NEVER overwrites coordinates
                    hub.heartbeat_node(hb_node_id)
                    await websocket.send_text(json.dumps({
                        "type": "heartbeat_ack",
                        "status": "ok",
                        "timestamp": time.time(),
                    }))
                elif msg_type == "update_location_response":
                    req_id = data.get("request_id")
                    hub.in_flight_position_updates.pop(actual_node_id, None)
                    fut = hub.pending_command_futures.pop(req_id, None)
                    if fut and not fut.done():
                        fut.set_result(data)
                elif msg_type == "batch":
                    batch_id = data.get("batch_id", "unknown")
                    count = await hub.ingest_batch(data)
                    # Send explicit batch_ack to authorize edge node to purge spool file
                    batch_ack = {
                        "type": "batch_ack",
                        "batch_id": batch_id,
                        "status": "committed",
                        "count": count,
                        "timestamp": time.time(),
                    }
                    await websocket.send_text(json.dumps(batch_ack))
                else:
                    await hub.ingest_packet(data)
                    hub.heartbeat_node(actual_node_id, packets_increment=1)

        except WebSocketDisconnect:
            logger.info(f"[*] Node '{actual_node_id}' disconnected from WebSocket stream.")
        except Exception as e:
            logger.warning(f"[!] WebSocket exception for node '{actual_node_id}': {e}")
        finally:
            if hub.active_node_connections.get(actual_node_id) is websocket:
                del hub.active_node_connections[actual_node_id]
            try:
                await websocket.close()
            except Exception:
                pass

    return app


# ============================================================================
# CLI Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Tactical Drone Remote ID - Central Ingestion Hub & Multi-Node Fusion Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8000, help="HTTP/WebSocket port")
    parser.add_argument("--db-file", type=str, default="rid_detections_central.db", help="Path to central SQLite DB")
    parser.add_argument("--log-dir", type=str, default="central_logs", help="Directory for cold forensic JSONL logs")
    parser.add_argument("--timeout-s", type=float, default=300.0, help="Flight encounter silence timeout in seconds")
    parser.add_argument("--quiet", action="store_true", help="Suppress verbose console logs")

    args = parser.parse_args()

    hub = CentralIngestionHub(
        db_path=args.db_file,
        log_dir=args.log_dir,
        timeout_s=args.timeout_s,
    )

    app = create_central_hub_app(hub)

    log_lvl = "warning" if args.quiet else "info"
    print(f"\n\033[1;32m🚀 DRONE REMOTE ID CENTRAL INGESTION HUB ACTIVE\033[0m")
    print(f"  • WebSocket Ingest : \033[1;36mws://{args.host}:{args.port}/stream/node\033[0m")
    print(f"  • REST API Endpoint: \033[1;36mhttp://{args.host}:{args.port}/api/nodes\033[0m")
    print(f"  • Central DB File  : \033[1;35m{os.path.abspath(args.db_file)}\033[0m")
    print(f"  • Forensic Logs Dir: \033[1;35m{os.path.abspath(args.log_dir)}\033[0m\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level=log_lvl)


if __name__ == "__main__":
    main()
