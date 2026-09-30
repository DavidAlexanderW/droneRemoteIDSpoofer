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

    def test_multi_node_deduplicator_primary_promotion(self):
        """Test that a subsequent duplicate packet with stronger RSSI promotes that node to primary."""
        dedup = MultiNodeDeduplicator(ttl_seconds=10.0)
        t0 = 1788810000.0

        # Node 1: Weak reception (-85 dBm)
        pkt1 = {
            "mac": "11:22:33:44:55:66",
            "serial": "DRONE-STRONG-TEST",
            "transport": "wifi",
            "timestamp": t0,
            "counter": 1,
            "node_id": "distant-node-01",
            "rssi_dbm": -85,
        }
        is_new1, rssi_map1, primary1 = dedup.process(pkt1)
        self.assertTrue(is_new1)
        self.assertEqual(primary1, "distant-node-01")

        # Node 2: Much stronger reception (-55 dBm) within same 0.5s window
        pkt2 = {
            "mac": "11:22:33:44:55:66",
            "serial": "DRONE-STRONG-TEST",
            "transport": "wifi",
            "timestamp": t0 + 0.02,
            "counter": 1,
            "node_id": "close-node-02",
            "rssi_dbm": -55,
        }
        is_new2, rssi_map2, primary2 = dedup.process(pkt2)
        self.assertFalse(is_new2)
        self.assertEqual(primary2, "close-node-02")  # Promoted!
        self.assertEqual(rssi_map2["close-node-02"], -55)
        self.assertEqual(rssi_map2["distant-node-01"], -85)

        # Node 3: Medium reception (-70 dBm) -> close-node-02 should remain primary
        pkt3 = {
            "mac": "11:22:33:44:55:66",
            "serial": "DRONE-STRONG-TEST",
            "transport": "wifi",
            "timestamp": t0 + 0.04,
            "counter": 1,
            "node_id": "mid-node-03",
            "rssi_dbm": -70,
        }
        is_new3, rssi_map3, primary3 = dedup.process(pkt3)
        self.assertFalse(is_new3)
        self.assertEqual(primary3, "close-node-02")
        self.assertEqual(len(rssi_map3), 3)

    def test_multi_node_deduplicator_temporal_windows(self):
        """Test 0.5s temporal quantisation boundaries."""
        dedup = MultiNodeDeduplicator(ttl_seconds=10.0)
        t0 = 1788810000.0

        pkt_base = {
            "mac": "AA:BB:CC:DD:EE:01",
            "transport": "wifi",
            "counter": 5,
            "node_id": "node-1",
            "rssi_dbm": -70,
        }

        # Packet at t0
        pkt1 = {**pkt_base, "timestamp": t0}
        self.assertTrue(dedup.process(pkt1)[0])

        # Packet at t0 + 0.15 -> rounds to 1788810000.0 (same window) -> duplicate
        pkt2 = {**pkt_base, "timestamp": t0 + 0.15, "node_id": "node-2"}
        self.assertFalse(dedup.process(pkt2)[0])

        # Packet at t0 + 0.55 -> rounds to 1788810000.5 (next window) -> unique
        pkt3 = {**pkt_base, "timestamp": t0 + 0.55}
        self.assertTrue(dedup.process(pkt3)[0])

        # Packet at t0 + 1.05 -> rounds to 1788810001.0 (subsequent window) -> unique
        pkt4 = {**pkt_base, "timestamp": t0 + 1.05}
        self.assertTrue(dedup.process(pkt4)[0])

    def test_multi_node_deduplicator_transports_and_counters(self):
        """Test that different transports or sequence counters are not falsely deduplicated."""
        dedup = MultiNodeDeduplicator(ttl_seconds=10.0)
        t0 = 1788810000.0

        base = {
            "mac": "99:88:77:66:55:44",
            "timestamp": t0,
            "node_id": "node-1",
            "rssi_dbm": -60,
        }

        # Wi-Fi Beacon
        pkt_wifi = {**base, "transport": "wifi", "counter": 1}
        self.assertTrue(dedup.process(pkt_wifi)[0])

        # BLE broadcast at same time with same counter -> MUST NOT be deduplicated
        pkt_ble = {**base, "transport": "bt5", "counter": 1}
        self.assertTrue(dedup.process(pkt_ble)[0])

        # Wi-Fi packet with incremented counter -> MUST NOT be deduplicated
        pkt_wifi_cnt2 = {**base, "transport": "wifi", "counter": 2}
        self.assertTrue(dedup.process(pkt_wifi_cnt2)[0])

    def test_multi_node_deduplicator_entity_resolution(self):
        """Test entity resolution prioritizing serial over MAC address."""
        dedup = MultiNodeDeduplicator(ttl_seconds=10.0)
        t0 = 1788810000.0

        # Drone A: MAC 1, Serial A
        pkt_a = {
            "mac": "11:11:11:11:11:11",
            "serial": "DRONE-SERIAL-A",
            "transport": "wifi",
            "timestamp": t0,
            "counter": 1,
            "node_id": "node-1",
        }
        self.assertTrue(dedup.process(pkt_a)[0])

        # Same Serial with different MAC (e.g. MAC rotation / multi-radio) -> deduplicated
        pkt_a_diff_mac = {
            "mac": "22:22:22:22:22:22",
            "serial": "DRONE-SERIAL-A",
            "transport": "wifi",
            "timestamp": t0 + 0.05,
            "counter": 1,
            "node_id": "node-2",
        }
        self.assertFalse(dedup.process(pkt_a_diff_mac)[0])

        # Only MAC without serial -> falls back to MAC
        pkt_no_serial = {
            "mac": "33:33:33:33:33:33",
            "transport": "wifi",
            "timestamp": t0,
            "counter": 1,
            "node_id": "node-1",
        }
        self.assertTrue(dedup.process(pkt_no_serial)[0])

    def test_multi_node_deduplicator_ttl_expiry(self):
        """Test cache eviction when TTL expires."""
        dedup = MultiNodeDeduplicator(ttl_seconds=0.1)
        t0 = 1788810000.0

        pkt = {
            "mac": "AA:BB:CC:DD:EE:FF",
            "transport": "wifi",
            "timestamp": t0,
            "counter": 1,
            "node_id": "node-1",
            "rssi_dbm": -70,
        }
        self.assertTrue(dedup.process(pkt)[0])

        # Same packet immediately -> duplicate
        self.assertFalse(dedup.process(pkt)[0])

        # Manually trigger cleanup past TTL
        dedup._cleanup(now=time.time() + 1.0)
        self.assertEqual(len(dedup.seen_packets), 0)

        # After eviction, identical packet key is accepted as new
        self.assertTrue(dedup.process(pkt)[0])

    def test_multi_node_deduplicator_none_rssi_handling(self):
        """Test packets with None RSSI do not raise exceptions."""
        dedup = MultiNodeDeduplicator(ttl_seconds=10.0)
        t0 = 1788810000.0

        pkt1 = {
            "mac": "AA:AA:AA:AA:AA:AA",
            "transport": "wifi",
            "timestamp": t0,
            "counter": 1,
            "node_id": "node-none-rssi",
            "rssi_dbm": None,
        }
        is_new1, rssi_map1, primary1 = dedup.process(pkt1)
        self.assertTrue(is_new1)
        self.assertEqual(primary1, "node-none-rssi")
        self.assertEqual(rssi_map1, {})

        pkt2 = {
            "mac": "AA:AA:AA:AA:AA:AA",
            "transport": "wifi",
            "timestamp": t0 + 0.05,
            "counter": 1,
            "node_id": "node-with-rssi",
            "rssi_dbm": -68,
        }
        is_new2, rssi_map2, primary2 = dedup.process(pkt2)
        self.assertFalse(is_new2)
        self.assertEqual(primary2, "node-with-rssi")
        self.assertEqual(rssi_map2, {"node-with-rssi": -68})

    def test_async_ingest_packet_and_encounter_tracker(self):
        """Test async ingest_packet invokes EncounterTracker off-thread without blocking."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            t0 = 1788810000.0
            pkt1 = {
                "mac": "FE:DC:BA:98:76:54",
                "serial_number": "ASYNC-ENC-DRONE-01",
                "transport": "wifi",
                "timestamp": t0,
                "counter": 1,
                "node_id": "node-alpha",
                "rssi_dbm": -64,
                "messages": [{
                    "msg_type": 1,
                    "msg_type_name": "Location/Vector",
                    "latitude": 47.3769,
                    "longitude": 8.5417,
                    "altitude_geodetic": 520.0,
                    "height_agl": 60.0,
                    "speed_horizontal": 12.5,
                }],
            }
            eid = loop.run_until_complete(self.hub.ingest_packet(pkt1))
            self.assertIsNotNone(eid)
            self.assertTrue(eid.startswith("ENC-"))

            # Duplicate packet from another node
            pkt2 = {
                "mac": "FE:DC:BA:98:76:54",
                "serial_number": "ASYNC-ENC-DRONE-01",
                "transport": "wifi",
                "timestamp": t0 + 0.05,
                "counter": 1,
                "node_id": "node-beta",
                "rssi_dbm": -58,  # Stronger
            }
            eid2 = loop.run_until_complete(self.hub.ingest_packet(pkt2))
            self.assertEqual(eid, eid2)
            self.assertEqual(pkt2["primary_node_id"], "node-beta")
            self.assertEqual(pkt2["node_rssi_map"], {"node-alpha": -64, "node-beta": -58})

            # Verify active encounter was tracked
            active_serials = [
                enc.get("serial_number") for enc in self.hub.encounter_tracker.active_encounters.values()
            ]
            self.assertIn("ASYNC-ENC-DRONE-01", active_serials)
        finally:
            loop.close()

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
        log_files = [f for f in os.listdir(self.log_dir) if f.endswith(".jsonl")]
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

    def test_node_position_update_via_api_and_websocket_command(self):
        # 1. Register node in DB
        self.hub.register_node(
            node_id="sensor-node-01",
            node_meta={"name": "Zurich Node", "latitude": 47.37, "longitude": 8.54, "altitude_m": 410.0},
        )

        # 2. Mock connected edge node websocket
        class MockEdgeWebSocket:
            def __init__(self, hub):
                self.hub = hub
                self.sent_messages = []

            async def send_text(self, text):
                data = json.loads(text)
                self.sent_messages.append(data)
                # Immediately simulate remote node ACK
                if data.get("type") == "update_location":
                    req_id = data.get("request_id")
                    fut = self.hub.pending_command_futures.get(req_id)
                    if fut and not fut.done():
                        fut.set_result({
                            "type": "update_location_response",
                            "request_id": req_id,
                            "node_id": "sensor-node-01",
                            "status": "ok",
                            "latitude": data.get("latitude"),
                            "longitude": data.get("longitude"),
                            "altitude_m": data.get("altitude_m"),
                        })

        mock_ws = MockEdgeWebSocket(self.hub)
        self.hub.active_node_connections["sensor-node-01"] = mock_ws

        # 3. Call API to update position
        resp = self.client.post("/api/nodes/sensor-node-01/position", json={
            "latitude": 47.395,
            "longitude": 8.565,
            "altitude_m": 480.0,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["remote_synced"])
        self.assertEqual(data["node"]["latitude"], 47.395)
        self.assertEqual(data["node"]["longitude"], 8.565)

        # Verify command was sent to edge node
        self.assertEqual(len(mock_ws.sent_messages), 1)
        cmd = mock_ws.sent_messages[0]
        self.assertEqual(cmd["type"], "update_location")
        self.assertEqual(cmd["node_id"], "sensor-node-01")
        self.assertEqual(cmd["latitude"], 47.395)
        self.assertEqual(cmd["longitude"], 8.565)

    def test_in_flight_guard_prevents_heartbeat_reversion(self):
        # 1. Register node with initial coordinates
        self.hub.register_node(
            node_id="sensor-node-guard",
            node_meta={"name": "Guard Node", "latitude": 47.37, "longitude": 8.54},
        )

        # 2. Engage in-flight update to (47.45, 8.65)
        self.hub.in_flight_position_updates["sensor-node-guard"] = {
            "latitude": 47.45,
            "longitude": 8.65,
            "altitude_m": 430.0,
            "timestamp": time.time(),
        }

        # 3. Incoming stale registration or packet meta arrives with old coordinates (47.37, 8.54)
        self.hub.register_node(
            node_id="sensor-node-guard",
            node_meta={"name": "Guard Node", "latitude": 47.37, "longitude": 8.54},
        )

        # 4. Verify coordinates in DB are protected and preserved at the target coordinates
        nodes = get_receiver_nodes(self.hub.db_conn)
        target_node = next(n for n in nodes if n["node_id"] == "sensor-node-guard")
        self.assertEqual(target_node["latitude"], 47.45)
        self.assertEqual(target_node["longitude"], 8.65)

        # 5. Routine heartbeat arrives - verify it touches liveness and NEVER changes coordinates
        self.hub.heartbeat_node("sensor-node-guard", packets_increment=1)
        nodes = get_receiver_nodes(self.hub.db_conn)
        target_node = next(n for n in nodes if n["node_id"] == "sensor-node-guard")
        self.assertEqual(target_node["latitude"], 47.45)
        self.assertEqual(target_node["longitude"], 8.65)

    def test_central_daily_replay_logger_batching_and_lifecycle(self):
        """Verifies CentralDailyReplayLogger batch writes, dynamic timestamping, and closure."""
        from scanner.central_hub import CentralDailyReplayLogger
        import tempfile
        import shutil

        temp_log_dir = tempfile.mkdtemp(prefix="test_replay_logger_")
        try:
            logger_inst = CentralDailyReplayLogger(log_dir=temp_log_dir, flush_interval_s=1.0, flush_batch_size=5)
            loop = asyncio.new_event_loop()

            # Log 3 packets (less than flush_batch_size=5)
            t0 = 1788800000.0
            for i in range(3):
                pkt = {
                    "timestamp": t0 + i,
                    "node_id": "test_batch_node",
                    "transport": "bt5",
                    "mac": "AA:BB:CC:11:22:33",
                    "serial_number": "BATCH_TEST_DRONE",
                    "rssi_dbm": -65 + i,
                }
                loop.run_until_complete(logger_inst.log_packet(pkt, encounter_id="ENC-TEST-001"))

            # Explicit flush and close
            logger_inst.flush()
            logger_inst.close()
            loop.close()

            files = [f for f in os.listdir(temp_log_dir) if f.endswith(".jsonl")]
            self.assertEqual(len(files), 1)
            with open(os.path.join(temp_log_dir, files[0]), "r", encoding="utf-8") as f:
                lines = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0]["serial"], "BATCH_TEST_DRONE")
            self.assertEqual(lines[0]["encounter_id"], "ENC-TEST-001")
        finally:
            shutil.rmtree(temp_log_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

