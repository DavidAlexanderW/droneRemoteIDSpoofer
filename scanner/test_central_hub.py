#!/usr/bin/env python3
"""
Unit and integration tests for scanner/central_hub.py:
1. Multi-node spatial and temporal deduplication
2. Node registration, heartbeats, and watermark updates in SQLite
3. Daily forensic replay JSONL logging
4. REST API health and node listing
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from scanner.central_hub import (
    CentralDailyReplayLogger,
    CentralIngestionHub,
    MultiNodeDeduplicator,
    create_central_hub_app,
)
from scanner.db import get_node_sync_watermark, get_receiver_nodes


class TestCentralHub(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_hub_")
        self.db_path = os.path.join(self.test_dir, "test_rid_central.db")
        self.log_dir = os.path.join(self.test_dir, "central_logs")
        self.hub = CentralIngestionHub(
            db_path=self.db_path,
            log_dir=self.log_dir,
            timeout_s=300.0,
        )
        self.app = create_central_hub_app(self.hub)
        self.client = TestClient(self.app)

    def tearDown(self):
        try:
            self.hub.db_conn.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_multi_node_deduplicator(self):
        dedup = MultiNodeDeduplicator(ttl_seconds=10.0)

        t0 = 1788810000.0
        # Node 1 reception
        pkt1 = {
            "mac": "AA:BB:CC:11:22:33",
            "transport": "wifi",
            "timestamp": t0,
            "counter": 42,
            "node_id": "etz-node-01",
            "rssi_dbm": -65,
        }
        is_new1, rssi_map1, primary1 = dedup.process(pkt1)
        self.assertTrue(is_new1)
        self.assertEqual(primary1, "etz-node-01")
        self.assertEqual(rssi_map1, {"etz-node-01": -65})

        # Node 2 reception of the EXACT same broadcast packet (simultaneous sighting)
        pkt2 = {
            "mac": "AA:BB:CC:11:22:33",
            "transport": "wifi",
            "timestamp": t0 + 0.05,  # within 0.5s window
            "counter": 42,
            "node_id": "hg-tower-02",
            "rssi_dbm": -82,
        }
        is_new2, rssi_map2, primary2 = dedup.process(pkt2)
        self.assertFalse(is_new2)  # Deduplicated!
        self.assertEqual(primary2, "etz-node-01")  # -65 dBm was stronger
        self.assertEqual(rssi_map2, {"etz-node-01": -65, "hg-tower-02": -82})

    def test_node_registration_and_watermark(self):
        # 1. Register Node
        self.hub.register_node(
            node_id="test-sensor-01",
            node_meta={
                "name": "ETZ Sensor Node",
                "latitude": 47.3774,
                "longitude": 8.5528,
                "altitude_m": 450.0,
                "range_rings_m": [500, 1000, 2500],
            },
        )

        nodes = get_receiver_nodes(self.hub.db_conn)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["node_id"], "test-sensor-01")
        self.assertEqual(nodes[0]["name"], "ETZ Sensor Node")
        self.assertEqual(nodes[0]["status"], "ONLINE")

        # 2. Watermark update
        self.assertEqual(self.hub.get_last_synced_epoch("test-sensor-01"), 0.0)
        self.hub.update_node_watermark("test-sensor-01", 1788810050.0, count=100)
        self.assertEqual(self.hub.get_last_synced_epoch("test-sensor-01"), 1788810050.0)

    def test_rest_endpoints(self):
        # Register a node
        self.hub.register_node(
            node_id="zurich-01",
            node_meta={"name": "Zurich Central", "latitude": 47.37, "longitude": 8.54, "altitude_m": 420.0},
        )

        # GET /api/health
        resp_health = self.client.get("/api/health")
        self.assertEqual(resp_health.status_code, 200)
        data_health = resp_health.json()
        self.assertEqual(data_health["status"], "healthy")

        # GET /api/nodes
        resp_nodes = self.client.get("/api/nodes")
        self.assertEqual(resp_nodes.status_code, 200)
        data_nodes = resp_nodes.json()
        self.assertEqual(data_nodes["count"], 1)
        self.assertEqual(data_nodes["nodes"][0]["node_id"], "zurich-01")

    def test_batch_ingestion_and_daily_log(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        t0 = 1788820000.0
        batch_payload = {
            "type": "batch",
            "batch_id": "batch_test_01",
            "node_id": "zurich-01",
            "node_meta": {"name": "Zurich 01"},
            "items": [
                {
                    "mac": "11:22:33:44:55:66",
                    "serial_number": "1596E123456789012345",
                    "timestamp": t0 + i,
                    "transport": "wifi",
                    "channel": 6,
                    "rssi_dbm": -70 + i,
                    "counter": i,
                    "messages": [
                        {
                            "type": "Location",
                            "lat": 47.378 + (i * 0.0001),
                            "lon": 8.525 + (i * 0.0001),
                            "geodetic_altitude_m": 500.0 + i,
                            "speed_mps": 15.0,
                            "direction_deg": 90,
                        }
                    ],
                }
                for i in range(10)
            ],
        }

        count = loop.run_until_complete(self.hub.ingest_batch(batch_payload))
        self.assertEqual(count, 10)

        # Verify encounter created in database
        active_encs = self.hub.encounter_tracker.active_encounters
        self.assertIn("11:22:33:44:55:66", active_encs)
        enc = active_encs["11:22:33:44:55:66"]
        self.assertEqual(enc["serial_number"], "1596E123456789012345")
        self.assertEqual(enc["packet_count"], 10)

        # Verify daily forensic log file exists
        log_files = os.listdir(self.log_dir)
        self.assertEqual(len(log_files), 1)
        log_path = os.path.join(self.log_dir, log_files[0])
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 10)

        loop.close()

    def test_node_heartbeat_keeps_online(self):
        # Register node
        self.hub.register_node(
            node_id="sensor-node-hb",
            node_meta={"name": "HB Sensor", "latitude": 47.37, "longitude": 8.54},
        )
        # Heartbeat node
        self.hub.heartbeat_node("sensor-node-hb", packets_increment=5)

        nodes = get_receiver_nodes(self.hub.db_conn)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["node_id"], "sensor-node-hb")
        self.assertEqual(nodes[0]["status"], "ONLINE")
        self.assertEqual(nodes[0]["packets_received_total"], 5)


if __name__ == "__main__":
    unittest.main()

