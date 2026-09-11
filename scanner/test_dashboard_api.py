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
                encounter_id, mac, serial_number, first_seen, first_seen_iso,
                last_seen, last_seen_iso, duration_s, packet_count, transports,
                channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps,
                max_rate_mbps, phy_rate_dist_json, min_rssi_dbm, max_rssi_dbm, avg_rssi_dbm,
                min_alt_m, max_alt_m, max_speed_mps, pilot_lat, pilot_lon, pilot_alt_m,
                operator_id, self_id_desc, trajectory_json, is_active
            ) VALUES (
                'enc_test_001',
                'AA:BB:CC:11:22:33',
                '1596E123456789012345',
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
            [47.3800, 8.5500, 500.0, 12.0, 180, 1725780000.0],
            [47.3780, 8.5500, 510.0, 12.5, 180, 1725780020.0],
        ]
        conn.execute("""
            INSERT INTO encounters (
                encounter_id, mac, serial_number, first_seen, first_seen_iso,
                last_seen, last_seen_iso, duration_s, packet_count, transports,
                channels, wifi_rates, dominant_rate_mbps, dominant_modulation, min_rate_mbps,
                max_rate_mbps, phy_rate_dist_json, min_rssi_dbm, max_rssi_dbm, avg_rssi_dbm,
                min_alt_m, max_alt_m, max_speed_mps, pilot_lat, pilot_lon, pilot_alt_m,
                operator_id, self_id_desc, trajectory_json, is_active
            ) VALUES (
                'enc_test_002',
                'DD:EE:FF:44:55:66',
                '1596E999999999999999',
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

        # Populate Mock JSONL Log
        with open(self.jsonl_path, "w") as f:
            f.write(json.dumps({
                "encounter_id": "enc_test_001",
                "time_offset_ms": 0,
                "timestamp_iso": "2026-09-08T10:06:40+00:00",
                "transport": "bt5",
                "channel": "37",
                "rssi_dbm": -68,
                "rate_mbps": 1.0,
                "modulation": "GFSK",
                "rate_desc": "1.0 Mbps (LE 1M GFSK)",
                "mac": "AA:BB:CC:11:22:33",
                "serial": "1596E123456789012345",
                "counter": 1,
                "messages_b64": [],
                "decoded_messages": [
                    {"type": "Basic ID", "id": "1596E123456789012345"},
                    {"type": "Location", "lat": 47.3769, "lon": 8.5417, "alt": 450.0},
                    {"type": "Operator ID", "operator_id": "CHE87astd57qkgc4"}
                ]
            }) + "\n")

        self.client = TestClient(app)

    def tearDown(self):
        self.temp_dir.cleanup()

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

    def test_get_encounter_details(self):
        """Verify individual encounter details and trajectory array."""
        resp = self.client.get("/api/encounters/enc_test_001")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["encounter_id"], "enc_test_001")
        self.assertEqual(data["serial_number"], "1596E123456789012345")
        self.assertEqual(data["operator_id"], "CHE87astd57qkgc4")
        self.assertEqual(data["dominant_rate_mbps"], 1.0)
        self.assertEqual(data["dominant_modulation"], "GFSK")
        self.assertEqual(len(data["trajectory"]), 3)
        self.assertEqual(data["pilot_lat"], 47.3765)

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

    def test_get_encounter_packets(self):
        """Verify Deep Packet Inspector packet stream retrieval."""
        resp = self.client.get("/api/encounters/enc_test_001/packets")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["encounter_id"], "enc_test_001")
        self.assertGreaterEqual(data["packet_count"], 1)
        pkt0 = data["packets"][0]
        self.assertEqual(pkt0["transport"], "bt5")
        self.assertEqual(pkt0["rate_desc"], "1.0 Mbps (LE 1M GFSK)")
        self.assertEqual(pkt0["modulation"], "GFSK")
        self.assertEqual(pkt0["rate_mbps"], 1.0)
        self.assertIn("decoded_messages", pkt0)

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
        self.assertEqual(len(lines), 4) # header + 3 fixes

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

    def test_scanner_config_disk_persistence(self):
        """Verify scanner station config load and persistent save to disk."""
        cfg_path = os.path.join(self.temp_dir.name, "scanner_config.json")
        os.environ["RID_SCANNER_CONFIG_PATH"] = cfg_path

        # 1. GET initial config (creates default on disk)
        resp = self.client.get("/api/config/scanner")
        self.assertEqual(resp.status_code, 200)
        initial_cfg = resp.json()
        self.assertIn("latitude", initial_cfg)
        self.assertIn("longitude", initial_cfg)
        self.assertTrue(os.path.exists(cfg_path))

        # 2. POST updated config to disk
        new_payload = {
            "name": "Custom Mobile Drone Defense Post",
            "latitude": 47.3850,
            "longitude": 8.5550,
            "altitude_m": 480.0,
            "range_rings_m": [300, 600, 1200, 3000],
            "show_range_rings": True,
            "enabled": True,
        }
        post_resp = self.client.post("/api/config/scanner", json=new_payload)
        self.assertEqual(post_resp.status_code, 200)
        post_data = post_resp.json()
        self.assertEqual(post_data["status"], "ok")
        self.assertEqual(post_data["scanner"]["name"], "Custom Mobile Drone Defense Post")
        self.assertEqual(post_data["scanner"]["latitude"], 47.3850)

        # 3. Verify disk file content directly
        with open(cfg_path, "r", encoding="utf-8") as f:
            disk_json = json.load(f)
        self.assertEqual(disk_json["name"], "Custom Mobile Drone Defense Post")
        self.assertEqual(disk_json["latitude"], 47.3850)
        self.assertEqual(disk_json["range_rings_m"], [300, 600, 1200, 3000])

        # 4. Verify encounter details API response
        enc_resp = self.client.get("/api/encounters/enc_test_001")
        self.assertEqual(enc_resp.status_code, 200)
        enc_data = enc_resp.json()
        self.assertIn("trajectory", enc_data)
        self.assertEqual(len(enc_data["trajectory"]), 3)

    def test_scanner_config_locked_on_disk(self):
        """Verify that locked: true on disk prevents modification via web REST API."""
        cfg_path = os.path.join(self.temp_dir.name, "scanner_config_locked.json")
        os.environ["RID_SCANNER_CONFIG_PATH"] = cfg_path

        # 1. Write locked config to disk directly
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                "name": "Fixed Tactical Station",
                "latitude": 47.3769,
                "longitude": 8.5417,
                "altitude_m": 450.0,
                "locked": True,
                "enabled": True,
            }, f)

        # 2. Verify GET returns locked: True
        resp = self.client.get("/api/config/scanner")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["locked"])

        # 3. Attempt POST update while locked -> must return 403 Forbidden
        attempt_update = {
            "name": "Hacked Station Name",
            "latitude": 40.0,
            "longitude": 10.0,
        }
        post_resp = self.client.post("/api/config/scanner", json=attempt_update)
        self.assertEqual(post_resp.status_code, 403)
        self.assertIn("locked in configuration file on disk", post_resp.json()["detail"])

        # 4. Unlock directly in disk file
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                "name": "Fixed Tactical Station",
                "latitude": 47.3769,
                "longitude": 8.5417,
                "altitude_m": 450.0,
                "locked": False,
                "enabled": True,
            }, f)

        # 5. Subsequent POST update should now succeed
        post_resp_unlocked = self.client.post("/api/config/scanner", json={"name": "Unlocked Station Name"})
        self.assertEqual(post_resp_unlocked.status_code, 200)
        self.assertEqual(post_resp_unlocked.json()["scanner"]["name"], "Unlocked Station Name")

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


if __name__ == "__main__":
    unittest.main()


