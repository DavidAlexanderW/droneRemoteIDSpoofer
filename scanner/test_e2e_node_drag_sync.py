#!/usr/bin/env python3
"""
End-to-end integration test for Remote ID scanner node repositioning:
Verifies that dragging an unlocked node marker on the tactical dashboard
persists new geodetic coordinates to the remote scanner node's disk (scanner_config.json),
updates active memory, and avoids race condition snap-backs caused by in-flight heartbeats.
"""

import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

import uvicorn
from fastapi.testclient import TestClient

from scanner.central_hub import CentralIngestionHub, create_central_hub_app
from scanner.dashboard.app import app as dashboard_app
from scanner.forwarder import CentralStreamForwarder
from scanner.scanner_config import load_scanner_config, save_scanner_config


def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestE2ENodeDragSync(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_e2e_drag_")
        self.hub_db_path = os.path.join(self.test_dir, "central_hub.db")
        self.hub_log_dir = os.path.join(self.test_dir, "hub_logs")
        self.dash_db_path = os.path.join(self.test_dir, "dashboard.db")
        self.spool_dir = os.path.join(self.test_dir, "spool")
        self.scanner_cfg_path = os.path.join(self.test_dir, "scanner_config.json")

        os.makedirs(self.spool_dir, exist_ok=True)

        # 1. Setup Central Hub App & Server
        self.hub = CentralIngestionHub(
            db_path=self.hub_db_path,
            log_dir=self.hub_log_dir,
            timeout_s=60.0,
        )
        self.hub_app = create_central_hub_app(self.hub)
        self.hub_port = find_free_port()

        hub_config = uvicorn.Config(self.hub_app, host="127.0.0.1", port=self.hub_port, log_level="error")
        self.hub_server = uvicorn.Server(hub_config)
        self.hub_thread = threading.Thread(target=self.hub_server.run, daemon=True)
        self.hub_thread.start()

        # Wait for Central Hub to be responsive
        self._wait_for_server(f"http://127.0.0.1:{self.hub_port}/api/health")

        # 2. Setup Dashboard Environment & Point to Central Hub
        os.environ["RID_HUB_HTTP_URL"] = f"http://127.0.0.1:{self.hub_port}"
        os.environ["RID_DB_PATH"] = self.dash_db_path

        self.dash_client = TestClient(dashboard_app)

    def tearDown(self):
        self.hub_server.should_exit = True
        self.hub_thread.join(timeout=3.0)
        try:
            self.hub.db_conn.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _wait_for_server(self, url: str, timeout: float = 5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.05)
        raise TimeoutError(f"Server at {url} did not respond within {timeout}s")

    def test_e2e_node_drag_persists_to_remote_node_disk(self):
        """
        Full E2E flow:
        1. Edge scanner node initializes with coordinates (47.3774, 8.5528) saved in scanner_config.json.
        2. CentralStreamForwarder connects via WebSocket to Central Hub.
        3. Dashboard sends POST /api/nodes/{node_id}/position with (47.3915, 8.5600).
        4. Central Hub dispatches 'update_location' command over WebSocket to edge node.
        5. Forwarder updates in-memory coords and writes to scanner_config.json on disk.
        6. Forwarder returns 'update_location_response' back over WebSocket.
        7. Dashboard receives confirmation with remote_synced=True.
        8. Routine heartbeats do NOT snap back or revert the coordinates.
        """
        # Step 1: Create initial scanner_config.json on remote node disk
        initial_cfg = {
            "node_id": "remote-sensor-01",
            "name": "Zurich Test Node",
            "latitude": 47.3774,
            "longitude": 8.5528,
            "altitude_m": 450.0,
            "locked": False,
            "range_rings_m": [500, 1000, 2500],
            "custom_metadata_tag": "sensor-alpha-build",
        }
        save_scanner_config(initial_cfg, self.scanner_cfg_path)

        # Step 2: Launch CentralStreamForwarder in background asyncio loop
        forwarder = CentralStreamForwarder(
            hub_ws_url=f"ws://127.0.0.1:{self.hub_port}/stream/node",
            node_id="remote-sensor-01",
            node_meta=load_scanner_config(self.scanner_cfg_path),
            config_path=self.scanner_cfg_path,
            spool_dir=self.spool_dir,
            quiet=True,
        )

        forwarder.start()

        # Wait until node is registered and active connection is tracked in Central Hub
        t0 = time.time()
        while time.time() - t0 < 5.0:
            if "remote-sensor-01" in self.hub.active_node_connections:
                break
            time.sleep(0.05)
        self.assertIn("remote-sensor-01", self.hub.active_node_connections)

        # Verify initial registration in Central Hub
        hub_nodes = self.hub.db_conn.execute(
            "SELECT latitude, longitude, altitude_m FROM receiver_nodes WHERE node_id = ?;",
            ("remote-sensor-01",),
        ).fetchone()
        self.assertIsNotNone(hub_nodes)
        self.assertAlmostEqual(float(hub_nodes["latitude"]), 47.3774, places=4)
        self.assertAlmostEqual(float(hub_nodes["longitude"]), 8.5528, places=4)

        # Step 3 & 4: Simulate Dashboard drag-and-drop: POST new coordinates
        new_lat = 47.3915
        new_lon = 8.5600
        new_alt = 482.0

        resp = self.dash_client.post(
            "/api/nodes/remote-sensor-01/position",
            json={"latitude": new_lat, "longitude": new_lon, "altitude_m": new_alt},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data.get("remote_synced"), f"Expected remote_synced=True, got: {data}")

        # Step 5: Verify new coordinates are written directly to scanner_config.json on disk!
        self.assertTrue(os.path.exists(self.scanner_cfg_path))
        with open(self.scanner_cfg_path, "r", encoding="utf-8") as f:
            persisted_cfg = json.load(f)

        self.assertAlmostEqual(persisted_cfg["latitude"], new_lat, places=4)
        self.assertAlmostEqual(persisted_cfg["longitude"], new_lon, places=4)
        self.assertAlmostEqual(persisted_cfg["altitude_m"], new_alt, places=1)
        # Verify non-coordinate custom metadata was preserved during atomic write
        self.assertEqual(persisted_cfg.get("custom_metadata_tag"), "sensor-alpha-build")

        # Step 6: Verify forwarder in-memory metadata was updated
        self.assertAlmostEqual(forwarder.node_meta["latitude"], new_lat, places=4)
        self.assertAlmostEqual(forwarder.node_meta["longitude"], new_lon, places=4)

        # Step 7: Verify Central Hub SQLite has updated coordinates
        hub_node_after = self.hub.db_conn.execute(
            "SELECT latitude, longitude, altitude_m FROM receiver_nodes WHERE node_id = ?;",
            ("remote-sensor-01",),
        ).fetchone()
        self.assertAlmostEqual(float(hub_node_after["latitude"]), new_lat, places=4)
        self.assertAlmostEqual(float(hub_node_after["longitude"]), new_lon, places=4)

        # Step 8: Simulate in-flight heartbeat from forwarder - verify it does NOT revert coordinates
        self.hub.heartbeat_node("remote-sensor-01", packets_increment=1)
        hub_node_hb = self.hub.db_conn.execute(
            "SELECT latitude, longitude FROM receiver_nodes WHERE node_id = ?;",
            ("remote-sensor-01",),
        ).fetchone()
        self.assertAlmostEqual(float(hub_node_hb["latitude"]), new_lat, places=4)
        self.assertAlmostEqual(float(hub_node_hb["longitude"]), new_lon, places=4)

        # Stop forwarder cleanly
        forwarder.stop()

    def test_locked_node_rejects_repositioning(self):
        """Verify that dragging a locked node returns 403 Forbidden and preserves disk config."""
        locked_cfg = {
            "node_id": "locked-sensor-99",
            "name": "Secured Node",
            "latitude": 47.3700,
            "longitude": 8.5400,
            "locked": True,
        }
        cfg_path = os.path.join(self.test_dir, "locked_scanner_config.json")
        save_scanner_config(locked_cfg, cfg_path)

        # Register as locked in Central Hub
        self.hub.register_node("locked-sensor-99", locked_cfg)

        # Attempt to move node via Dashboard API
        resp = self.dash_client.post(
            "/api/nodes/locked-sensor-99/position",
            json={"latitude": 47.5000, "longitude": 8.7000},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("locked", resp.json()["detail"].lower())

        # Verify disk configuration is completely unchanged
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg_on_disk = json.load(f)
        self.assertAlmostEqual(cfg_on_disk["latitude"], 47.3700, places=4)
        self.assertAlmostEqual(cfg_on_disk["longitude"], 8.5400, places=4)

    def test_reconnect_syncs_calibrated_position_to_disk(self):
        """
        Verify that when an edge node connects with older coordinates on disk,
        the Central Hub's handshake ACK delivers the calibrated position, and
        the forwarder automatically persists the calibrated coordinates to disk.
        """
        calibrated_lat = 47.4123
        calibrated_lon = 8.5834
        calibrated_alt = 490.0

        # Pre-seed Central Hub with calibrated coordinates for this node
        self.hub.register_node(
            node_id="calibrated-sensor-42",
            node_meta={
                "name": "Calibrated Node",
                "latitude": calibrated_lat,
                "longitude": calibrated_lon,
                "altitude_m": calibrated_alt,
                "locked": False,
            },
        )

        # Create older/unaligned scanner_config.json on disk
        cfg_path = os.path.join(self.test_dir, "reconnect_scanner_config.json")
        save_scanner_config({
            "node_id": "calibrated-sensor-42",
            "name": "Calibrated Node",
            "latitude": 47.3700,
            "longitude": 8.5400,
            "altitude_m": 410.0,
            "locked": False,
        }, cfg_path)

        # Start forwarder with older config
        forwarder = CentralStreamForwarder(
            hub_ws_url=f"ws://127.0.0.1:{self.hub_port}/stream/node",
            node_id="calibrated-sensor-42",
            node_meta=load_scanner_config(cfg_path),
            config_path=cfg_path,
            spool_dir=self.spool_dir,
            quiet=True,
        )
        forwarder.start()

        # Wait for handshake completion
        t0 = time.time()
        while time.time() - t0 < 5.0:
            if "calibrated-sensor-42" in self.hub.active_node_connections:
                break
            time.sleep(0.05)
        self.assertIn("calibrated-sensor-42", self.hub.active_node_connections)

        # Give forwarder moment to apply handshake calibration
        time.sleep(0.2)

        # Verify disk file was updated with calibrated coordinates from Hub!
        with open(cfg_path, "r", encoding="utf-8") as f:
            disk_cfg = json.load(f)
        self.assertAlmostEqual(disk_cfg["latitude"], calibrated_lat, places=4)
        self.assertAlmostEqual(disk_cfg["longitude"], calibrated_lon, places=4)
        self.assertAlmostEqual(disk_cfg["altitude_m"], calibrated_alt, places=1)

        # Verify in-memory coordinates also match
        self.assertAlmostEqual(forwarder.node_meta["latitude"], calibrated_lat, places=4)
        self.assertAlmostEqual(forwarder.node_meta["longitude"], calibrated_lon, places=4)

        forwarder.stop()


if __name__ == "__main__":
    unittest.main()
