#!/usr/bin/env python3
"""
Automated unit tests for Tactical Drone Remote ID Dashboard API & Static Serving.
Tests REST endpoints, exports, and WebSocket live updates.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Ensure repo and scanner paths in sys.path
scanner_dir = os.path.abspath(os.path.dirname(__file__))
repo_root = os.path.abspath(os.path.join(scanner_dir, ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
if scanner_dir not in sys.path:
    sys.path.insert(0, scanner_dir)

from dashboard.app import app


class TestDashboardAPI(unittest.TestCase):
    def setUp(self):
        # Create a temporary SQLite database and JSONL log
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_rid.db")
        self.jsonl_path = os.path.join(self.temp_dir.name, "test_packets.jsonl")

        # Set environment variables for dashboard app
        os.environ["RID_DB_PATH"] = self.db_path
        os.environ["RID_JSONL_PATH"] = self.jsonl_path
        os.environ["RID_TIMEOUT_S"] = "300.0"
        os.environ.pop("RID_DASHBOARD_CONFIG_PATH", None)
        os.environ.pop("RID_SCANNER_CONFIG_PATH", None)

        # Initialize SQLite test database with realistic mock flight encounters
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        from db import init_encounters_db
        init_encounters_db(conn)

        # Insert Mock Encounter 1: Active BLE5 Flight with Public Operator ID
        traj_1 = [
            [47.3769, 8.5417, 450.0, 5.2, 90, 1725790000.0],
            [47.3770, 8.5420, 455.0, 6.1, 88, 1725790002.0],
            [47.3772, 8.5425, 460.0, 7.0, 85, 1725790004.0],
        ]
        conn.execute("""
            INSERT INTO encounters (
                encounter_id, mac, serial_number, node_id, first_seen, first_seen_iso,
                last_seen, last_seen_iso, duration_s, packet_count, transports,
                channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps,
                max_rate_mbps, phy_rate_dist_json, min_rssi_dbm, max_rssi_dbm, avg_rssi_dbm,
                min_alt_m, max_alt_m, max_speed_mps, pilot_lat, pilot_lon, pilot_alt_m,
                operator_id, self_id_desc, trajectory_json, is_active
            ) VALUES (
                'enc_test_001',
                'AA:BB:CC:11:22:33',
                '1596E123456789012345',
                'sensor-node-01',
                1725790000.0,
                '2026-09-08T10:06:40+00:00',
                1725790004.0,
                '2026-09-08T10:06:44+00:00',
                4.0,
                15,
                'bt5',
                '37,38,39',
                '1.0 Mbps (LE 1M GFSK)',
                1.0,
                'GFSK',
                1.0,
                1.0,
                '{"1.0 Mbps (LE 1M GFSK)": {"count": 15, "rate_mbps": 1.0, "modulation": "GFSK", "percent": 100.0}}',
                -75,
                -62,
                -68.5,
                450.0,
                460.0,
                7.0,
                47.3765,
                8.5410,
                430.0,
                'CHE87astd57qkgc4',
                'Survey Mission Alpha',
                ?,
                1
            )
        """, (json.dumps(traj_1),))

        # Insert Mock Encounter 2: Closed Wi-Fi Flight
        traj_2 = [
            [47.3800, 8.5500, 500.0, 12.0, 180, 1725780000.0, 80.0, 0, 480.0, 0.5, -82.0, 10],
            [47.3780, 8.5500, 510.0, 12.5, 180, 1725780020.0, 90.0, 0, 490.0, 0.5, -74.0, 11],
        ]
        conn.execute("""
            INSERT INTO encounters (
                encounter_id, mac, serial_number, node_id, first_seen, first_seen_iso,
                last_seen, last_seen_iso, duration_s, packet_count, transports,
                channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps,
                max_rate_mbps, phy_rate_dist_json, min_rssi_dbm, max_rssi_dbm, avg_rssi_dbm,
                min_alt_m, max_alt_m, max_speed_mps, pilot_lat, pilot_lon, pilot_alt_m,
                operator_id, self_id_desc, trajectory_json, is_active
            ) VALUES (
                'enc_test_002',
                'DD:EE:FF:44:55:66',
                '1596E999999999999999',
                'sensor-node-02',
                1725780000.0,
                '2026-09-08T07:20:00+00:00',
                1725780020.0,
                '2026-09-08T07:20:20+00:00',
                20.0,
                40,
                'wifi',
                '6',
                '1.0 Mbps DSSS (90%), 6.0 Mbps OFDM (10%)',
                1.0,
                'DSSS',
                1.0,
                6.0,
                '{"1.0 Mbps DSSS": {"count": 36, "rate_mbps": 1.0, "modulation": "DSSS", "percent": 90.0}, "6.0 Mbps OFDM": {"count": 4, "rate_mbps": 6.0, "modulation": "OFDM", "percent": 10.0}}',
                -85,
                -70,
                -78.0,
                500.0,
                510.0,
                12.5,
                NULL,
                NULL,
                NULL,
                NULL,
                'Delivery Drone Test',
                ?,
                0
            )
        """, (json.dumps(traj_2),))

        conn.commit()
        conn.close()

        # Populate Mock JSONL Log with raw base64 ASTM F3411 blocks (with realistic sniffer-start absolute offsets)
        with open(self.jsonl_path, "w") as f:
            f.write(json.dumps({
                "encounter_id": "enc_test_001",
                "time_offset_ms": 242560000,
                "timestamp_iso": "2026-09-08T10:06:40+00:00",
                "transport": "bt5",
                "channel": "37",
                "rssi_dbm": -68,
                "rate_mbps": 1.0,
                "modulation": "GFSK",
                "rate_desc": "1.0 Mbps (LE 1M GFSK)",
                "mac": "AA:BB:CC:11:22:33",
                "serial": "1581F5FHC255A00E90R6",
                "counter": 4,
                # Real base64 chunks for Basic ID and System messages
                "messages_b64": [
                    "ARIxNTgxRjVGSEMyNTVBMDBFOTBSNgAAAA==",  # Basic ID
                    "QQV8qjwccaEYBQEAAAAAAAADAAAAAAAAAA==",  # System (Pilot)
                ],
            }) + "\n")
            f.write(json.dumps({
                "encounter_id": "enc_test_001",
                "time_offset_ms": 242561500,
                "timestamp_iso": "2026-09-08T10:06:41.500+00:00",
                "transport": "bt5",
                "channel": "38",
                "rssi_dbm": -65,
                "rate_mbps": 1.0,
                "modulation": "GFSK",
                "rate_desc": "1.0 Mbps (LE 1M GFSK)",
                "mac": "AA:BB:CC:11:22:33",
                "serial": "1581F5FHC255A00E90R6",
                "counter": 5,
                "messages_b64": [
                    "ARIxNTgxRjVGSEMyNTVBMDBFOTBSNgAAAA==",
                ],
            }) + "\n")

        self.client = TestClient(app)

    def tearDown(self):
        self.temp_dir.cleanup()
        os.environ.pop("RID_DASHBOARD_CONFIG_PATH", None)
        os.environ.pop("RID_SCANNER_CONFIG_PATH", None)
        os.environ.pop("RID_DB_PATH", None)
        os.environ.pop("RID_JSONL_PATH", None)
        os.environ.pop("RID_TIMEOUT_S", None)

    def test_get_stats(self):
        """Verify global airspace statistics calculation."""
        resp = self.client.get("/api/stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total_encounters"], 2)
        self.assertEqual(data["total_packets"], 55)
        self.assertEqual(data["unique_macs"], 2)
        self.assertEqual(data["unique_operators"], 1)
        self.assertIn("transports_breakdown", data)
        self.assertEqual(data["transports_breakdown"]["bt5"], 15)
        self.assertEqual(data["transports_breakdown"]["wifi"], 40)

    def test_get_encounters_feed(self):
        """Verify encounter list querying, search, and filtering."""
        # 1. All encounters
        resp = self.client.get("/api/encounters")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)

        # 2. Filter by search (Serial)
        resp_search = self.client.get("/api/encounters?search=1596E123456789012345")
        self.assertEqual(resp_search.status_code, 200)
        d_search = resp_search.json()
        self.assertEqual(d_search["count"], 1)
        self.assertEqual(d_search["encounters"][0]["encounter_id"], "enc_test_001")
        self.assertEqual(d_search["encounters"][0]["operator_id"], "CHE87astd57qkgc4")

        # 3. Filter by Operator ID
        resp_op = self.client.get("/api/encounters?operator=CHE87")
        self.assertEqual(resp_op.status_code, 200)
        d_op = resp_op.json()
        self.assertEqual(d_op["count"], 1)
        self.assertEqual(d_op["encounters"][0]["operator_id"], "CHE87astd57qkgc4")
        self.assertEqual(d_op["encounters"][0]["node_id"], "sensor-node-01")

        # 4. Filter by Node ID
        resp_node = self.client.get("/api/encounters?node_id=sensor-node-02")
        self.assertEqual(resp_node.status_code, 200)
        d_node = resp_node.json()
        self.assertEqual(d_node["count"], 1)
        self.assertEqual(d_node["encounters"][0]["encounter_id"], "enc_test_002")
        self.assertEqual(d_node["encounters"][0]["node_id"], "sensor-node-02")

    def test_get_encounter_details(self):
        """Verify individual encounter details and trajectory array."""
        resp = self.client.get("/api/encounters/enc_test_001")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["encounter_id"], "enc_test_001")
        self.assertEqual(data["node_id"], "sensor-node-01")
        self.assertEqual(data["serial_number"], "1596E123456789012345")
        self.assertEqual(data["operator_id"], "CHE87astd57qkgc4")
        self.assertEqual(data["dominant_rate_mbps"], 1.0)
        self.assertEqual(data["dominant_modulation"], "GFSK")
        self.assertEqual(len(data["trajectory"]), 3)
        self.assertEqual(data["pilot_lat"], 47.3765)
        self.assertIn("conformance_blocks", data)
        self.assertEqual(data["conformance_blocks"]["basic_id"], "passed")
        self.assertEqual(data["conformance_blocks"]["location"], "passed")
        self.assertEqual(data["conformance_blocks"]["system"], "passed")
        self.assertEqual(data["conformance_blocks"]["operator"], "passed")

        # Also verify Encounter 2 (Wi-Fi with 90% DSSS and 10% OFDM)
        resp2 = self.client.get("/api/encounters/enc_test_002")
        self.assertEqual(resp2.status_code, 200)
        d2 = resp2.json()
        self.assertEqual(d2["dominant_rate_mbps"], 1.0)
        self.assertEqual(d2["dominant_modulation"], "DSSS")
        self.assertEqual(d2["min_rate_mbps"], 1.0)
        self.assertEqual(d2["max_rate_mbps"], 6.0)
        self.assertIn("1.0 Mbps DSSS", d2["phy_rate_distribution"])
        self.assertEqual(d2["phy_rate_distribution"]["1.0 Mbps DSSS"]["percent"], 90.0)
        self.assertEqual(d2["phy_rate_distribution"]["6.0 Mbps OFDM"]["percent"], 10.0)
        self.assertIn("conformance_blocks", d2)

    def test_get_encounter_packets(self):
        """Verify Deep Packet Inspector packet stream retrieval and ASTM block decoding."""
        # 1. Test encounter with raw base64 messages in JSONL log
        resp = self.client.get("/api/encounters/enc_test_001/packets")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["encounter_id"], "enc_test_001")
        self.assertEqual(data["packet_count"], 2)
        pkt0 = data["packets"][0]
        self.assertEqual(pkt0["transport"], "bt5")
        self.assertEqual(pkt0["rate_desc"], "1.0 Mbps (LE 1M GFSK)")
        self.assertEqual(pkt0["modulation"], "GFSK")
        self.assertEqual(pkt0["rate_mbps"], 1.0)
        self.assertEqual(pkt0["time_offset_ms"], 0) # Normalized relative offset
        self.assertEqual(pkt0["counter"], 4)        # Exact ASTM Sequence Counter
        self.assertIn("decoded_messages", pkt0)
        self.assertEqual(len(pkt0["decoded_messages"]), 2)
        
        # Verify second packet normalized offset and counter
        pkt1 = data["packets"][1]
        self.assertEqual(pkt1["time_offset_ms"], 1500)
        self.assertEqual(pkt1["counter"], 5)
        
        # Verify decoded Basic ID block
        basic_id_msg = pkt0["decoded_messages"][0]
        self.assertEqual(basic_id_msg["type"], "Basic ID")
        self.assertEqual(basic_id_msg["id"], "1581F5FHC255A00E90R6")
        self.assertEqual(basic_id_msg["ua_type_name"], "Helicopter / Multirotor")
        
        # Verify decoded System block
        sys_msg = pkt0["decoded_messages"][1]
        self.assertEqual(sys_msg["type"], "System")
        self.assertAlmostEqual(sys_msg["pilot_lat"], 47.37378, places=4)
        self.assertAlmostEqual(sys_msg["pilot_lon"], 8.55002, places=4)

        # 2. Test encounter synthesized from SQLite trajectory points
        resp2 = self.client.get("/api/encounters/enc_test_002/packets")
        self.assertEqual(resp2.status_code, 200)
        d2 = resp2.json()
        self.assertEqual(d2["packet_count"], 2)
        pkt_synth0 = d2["packets"][0]
        self.assertEqual(pkt_synth0["rssi_dbm"], -82.0)
        self.assertEqual(pkt_synth0["time_offset_ms"], 0)
        self.assertEqual(pkt_synth0["counter"], 10)
        pkt_synth1 = d2["packets"][1]
        self.assertEqual(pkt_synth1["rssi_dbm"], -74.0)
        self.assertEqual(pkt_synth1["time_offset_ms"], 20000)
        self.assertEqual(pkt_synth1["counter"], 11)
        self.assertIn("decoded_messages", pkt_synth0)
        self.assertTrue(any(m["type"] == "Location" for m in pkt_synth0["decoded_messages"]))
        loc_msg = [m for m in pkt_synth0["decoded_messages"] if m["type"] == "Location"][0]
        self.assertEqual(loc_msg["lat"], 47.3800)
        self.assertEqual(loc_msg["lon"], 8.5500)
        self.assertEqual(loc_msg["alt"], 500.0)

    def test_export_geojson(self):
        """Verify GeoJSON RFC 7946 export format."""
        resp = self.client.get("/api/export/enc_test_001/geojson")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/geo+json")
        geojson_doc = resp.json()
        self.assertEqual(geojson_doc["type"], "FeatureCollection")
        self.assertEqual(geojson_doc["properties"]["operator_id"], "CHE87astd57qkgc4")
        # LineString + 3 Points + Pilot Point = 5 features
        self.assertEqual(len(geojson_doc["features"]), 5)
        self.assertEqual(geojson_doc["features"][0]["geometry"]["type"], "LineString")

    def test_export_csv(self):
        """Verify CSV telemetry export format."""
        resp = self.client.get("/api/export/enc_test_001/csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["content-type"])
        lines = resp.text.strip().split("\n")
        self.assertTrue(lines[0].startswith("index,timestamp_epoch,latitude,longitude"))
        self.assertTrue(lines[0].endswith("msg_counter"))
        self.assertEqual(len(lines), 4) # header + 3 fixes

        # Verify Encounter 2 CSV has point-level RSSI and msg_counter values
        resp2 = self.client.get("/api/export/enc_test_002/csv")
        self.assertEqual(resp2.status_code, 200)
        lines2 = resp2.text.strip().split("\n")
        self.assertEqual(len(lines2), 3) # header + 2 fixes
        self.assertTrue(lines2[1].endswith("-82.0,10"))
        self.assertTrue(lines2[2].endswith("-74.0,11"))

    def test_static_ui_serving(self):
        """Verify static HTML and CSS asset serving."""
        resp_html = self.client.get("/")
        self.assertEqual(resp_html.status_code, 200)
        self.assertIn("AIRSPACE OBSERVATION", resp_html.text)

        resp_css = self.client.get("/css/style.css")
        self.assertEqual(resp_css.status_code, 200)
        self.assertIn("tactical-dark", resp_css.text)

    def test_websocket_live_stream(self):
        """Verify WebSocket /ws/live telemetry pushes."""
        with self.client.websocket_connect("/ws/live") as websocket:
            data = websocket.receive_json()
            self.assertEqual(data["type"], "live_telemetry")
            self.assertIn("active_drones", data)
            self.assertIn("active_count", data)

    def test_node_position_update_unlocked(self):
        """Verify position update for an unlocked receiver node via POST /api/nodes/{node_id}/position."""
        from db import upsert_receiver_node
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        upsert_receiver_node(
            conn,
            node_id="node-test-unlocked",
            name="Alpha Mobile Scanner",
            latitude=47.3769,
            longitude=8.5417,
            altitude_m=450.0,
            locked=False,
        )
        conn.close()

        # Update position via API
        resp = self.client.post("/api/nodes/node-test-unlocked/position", json={
            "latitude": 47.3900,
            "longitude": 8.5600,
            "altitude_m": 475.0,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["node"]["latitude"], 47.3900)
        self.assertEqual(data["node"]["longitude"], 8.5600)
        self.assertEqual(data["node"]["altitude_m"], 475.0)

    def test_node_position_update_locked(self):
        """Verify that position update for a locked receiver node returns 403 Forbidden."""
        from db import upsert_receiver_node
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        upsert_receiver_node(
            conn,
            node_id="node-test-locked",
            name="Fixed Mast Radar",
            latitude=47.3769,
            longitude=8.5417,
            altitude_m=450.0,
            locked=True,
        )
        conn.close()

        # Attempt to update position via API
        resp = self.client.post("/api/nodes/node-test-locked/position", json={
            "latitude": 40.0,
            "longitude": 10.0,
        })
        self.assertEqual(resp.status_code, 403)
        self.assertIn("locked on disk", resp.json()["detail"])

    def test_dashboard_config_disk_persistence(self):
        """Verify dashboard viewport config load and persistent save to disk via /api/config/dashboard."""
        cfg_path = os.path.join(self.temp_dir.name, "dashboard_config.json")
        os.environ["RID_DASHBOARD_CONFIG_PATH"] = cfg_path

        # 1. GET initial dashboard config (creates default on disk)
        resp = self.client.get("/api/config/dashboard")
        self.assertEqual(resp.status_code, 200)
        initial_cfg = resp.json()
        self.assertIn("center_latitude", initial_cfg)
        self.assertIn("center_longitude", initial_cfg)
        self.assertIn("title", initial_cfg)
        self.assertTrue(os.path.exists(cfg_path))

        # 2. POST updated dashboard config to disk
        new_payload = {
            "title": "Central Command Airspace Radar",
            "center_latitude": 46.9480,
            "center_longitude": 7.4474,
            "default_zoom": 14,
            "show_range_rings": True,
            "show_trails": True,
            "show_waypoints": True,
        }
        post_resp = self.client.post("/api/config/dashboard", json=new_payload)
        self.assertEqual(post_resp.status_code, 200)
        post_data = post_resp.json()
        self.assertEqual(post_data["status"], "ok")
        self.assertEqual(post_data["dashboard"]["center_latitude"], 46.9480)
        self.assertEqual(post_data["dashboard"]["center_longitude"], 7.4474)
        self.assertEqual(post_data["dashboard"]["title"], "Central Command Airspace Radar")
        self.assertEqual(post_data["dashboard"]["default_zoom"], 14)

        # 3. Verify disk file content directly
        with open(cfg_path, "r", encoding="utf-8") as f:
            disk_json = json.load(f)
        self.assertEqual(disk_json["title"], "Central Command Airspace Radar")
        self.assertEqual(disk_json["center_latitude"], 46.9480)
        self.assertEqual(disk_json["center_longitude"], 7.4474)
        self.assertEqual(disk_json["default_zoom"], 14)

    def test_faa_lookup_endpoint(self):
        """Verify FAA DOC registry lookup proxy endpoint."""
        from unittest.mock import MagicMock, patch

        # 1. Test missing serial -> 400
        resp_bad = self.client.get("/api/faa_lookup?serial=")
        self.assertEqual(resp_bad.status_code, 400)

        # 2. Test successful lookup with mocked FAA server response
        mock_faa_response = {
            "data": {
                "items": [
                    {
                        "makeName": "DJI",
                        "modelName": "Mavic 3 Pro",
                        "series": "Mavic 3 Series",
                        "trackingNumber": "RID000000456",
                        "status": "ACCEPTED",
                        "category": "Standard Remote ID Drone",
                        "applicantName": "SZ DJI TECHNOLOGY CO., LTD"
                    }
                ]
            }
        }

        mock_resp_obj = MagicMock()
        mock_resp_obj.status = 200
        mock_resp_obj.read.return_value = json.dumps(mock_faa_response).encode("utf-8")
        mock_resp_obj.__enter__.return_value = mock_resp_obj
        mock_resp_obj.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=mock_resp_obj):
            resp = self.client.get("/api/faa_lookup?serial=1581F4TEST001")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["found"])
            self.assertEqual(data["make"], "DJI")
            self.assertEqual(data["model"], "Mavic 3 Pro")
            self.assertEqual(data["series"], "Mavic 3 Series")
            self.assertEqual(data["tracking_number"], "RID000000456")
            self.assertEqual(data["doc_status"], "ACCEPTED")

        # 3. Test not found response
        mock_empty_resp = {"data": {"items": []}}
        mock_empty_obj = MagicMock()
        mock_empty_obj.status = 200
        mock_empty_obj.read.return_value = json.dumps(mock_empty_resp).encode("utf-8")
        mock_empty_obj.__enter__.return_value = mock_empty_obj
        mock_empty_obj.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=mock_empty_obj):
            resp_nf = self.client.get("/api/faa_lookup?serial=NONEXISTENT_SERIAL_999")
            self.assertEqual(resp_nf.status_code, 200)
            data_nf = resp_nf.json()
            self.assertFalse(data_nf["found"])
            self.assertIn("No matching Declaration of Compliance", data_nf["message"])

    def test_db_path_resolution(self):
        from dashboard.app import get_db_path
        # 1. When RID_DB_PATH is set
        os.environ["RID_DB_PATH"] = "/custom/path/detections.db"
        self.assertEqual(get_db_path(), "/custom/path/detections.db")

        # 2. When RID_DB_PATH is unset and central DB exists
        os.environ.pop("RID_DB_PATH", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            central_db = os.path.join(tmpdir, "rid_detections_central.db")
            with open(central_db, "w") as f:
                f.write("")
            with patch("dashboard.app.repo_root", tmpdir):
                resolved = get_db_path()
                self.assertEqual(resolved, os.path.abspath(central_db))


if __name__ == "__main__":
    unittest.main()



