#!/usr/bin/env python3
"""
Unit tests for ANSI/CTA-2063-A Drone Remote ID Model Inference & DB Persistence.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure repo and scanner paths in sys.path
scanner_dir = os.path.abspath(os.path.dirname(__file__))
repo_root = os.path.abspath(os.path.join(scanner_dir, ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
if scanner_dir not in sys.path:
    sys.path.insert(0, scanner_dir)

from drone_models import infer_drone_model, CTA_MANUFACTURERS, MODEL_PREFIX_MAP
from combined_rid_listener import EncounterTracker


class TestDroneModelInference(unittest.TestCase):
    def test_dji_model_inference(self):
        # DJI Mavic 3 family (1581F5...)
        res = infer_drone_model("1581F5NQC23456789012")
        self.assertTrue(res["is_inferred"])
        self.assertEqual(res["make"], "DJI")
        self.assertIn("Mavic 3", res["model"])
        self.assertEqual(res["country"], "China")

        # DJI Mini 4 Pro / Air 3 family (1581F9...)
        res2 = infer_drone_model("1581F9ABC12345678901")
        self.assertTrue(res2["is_inferred"])
        self.assertEqual(res2["make"], "DJI")
        self.assertIn("Mini 4 Pro", res2["model"])

        # DJI Matrice Enterprise (1581F8...)
        res3 = infer_drone_model("1581F8DEF12345678901")
        self.assertTrue(res3["is_inferred"])
        self.assertEqual(res3["make"], "DJI")
        self.assertIn("Matrice 30", res3["model"])

    def test_autel_model_inference(self):
        # Autel EVO II Pro (1596E1...)
        res = infer_drone_model("1596E123456789012345")
        self.assertTrue(res["is_inferred"])
        self.assertEqual(res["make"], "Autel Robotics")
        self.assertIn("EVO II", res["model"])

    def test_skydio_and_parrot_inference(self):
        # Skydio (1668...)
        res_skydio = infer_drone_model("1668A123456789012345")
        self.assertTrue(res_skydio["is_inferred"])
        self.assertEqual(res_skydio["make"], "Skydio")
        self.assertEqual(res_skydio["country"], "United States")

        # Parrot (1748...)
        res_parrot = infer_drone_model("1748B123456789012345")
        self.assertTrue(res_parrot["is_inferred"])
        self.assertEqual(res_parrot["make"], "Parrot")
        self.assertEqual(res_parrot["country"], "France")

    def test_swiss_drones_inference(self):
        # Wingtra (1686...)
        res_wingtra = infer_drone_model("1686W123456789012345")
        self.assertTrue(res_wingtra["is_inferred"])
        self.assertEqual(res_wingtra["make"], "Wingtra")
        self.assertEqual(res_wingtra["country"], "Switzerland")

        # Flyability (1716...)
        res_fly = infer_drone_model("1716F123456789012345")
        self.assertTrue(res_fly["is_inferred"])
        self.assertEqual(res_fly["make"], "Flyability")
        self.assertEqual(res_fly["country"], "Switzerland")

    def test_dronetag_inference(self):
        # Dronetag (1714...)
        res_dt = infer_drone_model("1714D123456789012345")
        self.assertTrue(res_dt["is_inferred"])
        self.assertEqual(res_dt["make"], "Dronetag")
        self.assertEqual(res_dt["country"], "Czech Republic")

    def test_invalid_and_unknown_serials(self):
        self.assertFalse(infer_drone_model(None)["is_inferred"])
        self.assertFalse(infer_drone_model("")["is_inferred"])
        self.assertFalse(infer_drone_model("123")["is_inferred"])
        self.assertFalse(infer_drone_model("9999UNKNOWN123456")["is_inferred"])


class TestDroneModelDatabasePersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_drone_models.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_encounter_tracker_auto_infers_and_persists_model(self):
        tracker = EncounterTracker(db_path=self.db_path, persist_interval_s=0.0)
        packet = {
            "mac": "11:22:33:44:55:66",
            "serial_number": "1581F5M3TEST12345678",
            "transport": "bt5",
            "channel": 37,
            "rssi_dbm": -65,
            "timestamp": 1725800000.0,
            "messages": [
                {
                    "type": "Basic ID",
                    "id": "1581F5M3TEST12345678",
                    "id_type": 1,
                    "ua_type": 2
                },
                {
                    "type": "Location",
                    "lat": 47.3769,
                    "lon": 8.5417,
                    "geodetic_altitude_m": 500.0,
                    "speed_mps": 12.0,
                    "direction_deg": 180,
                }
            ]
        }
        enc_id = tracker.update_with_packet(packet)
        tracker.finalize_all()

        # Check SQLite DB
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (enc_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["drone_make"], "DJI")
            self.assertIn("Mavic 3", row["drone_model"])

    def test_database_schema_migration_and_backfill(self):
        # Create legacy table without drone_make / drone_model columns
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE encounters (
                    encounter_id TEXT PRIMARY KEY,
                    mac TEXT NOT NULL,
                    serial_number TEXT,
                    first_seen REAL NOT NULL,
                    first_seen_iso TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    last_seen_iso TEXT NOT NULL,
                    duration_s REAL NOT NULL,
                    packet_count INTEGER NOT NULL,
                    transports TEXT NOT NULL,
                    channels TEXT NOT NULL,
                    min_rssi_dbm INTEGER,
                    max_rssi_dbm INTEGER,
                    avg_rssi_dbm REAL,
                    min_alt_m REAL,
                    max_alt_m REAL,
                    max_speed_mps REAL,
                    pilot_lat REAL,
                    pilot_lon REAL,
                    pilot_alt_m REAL,
                    operator_id TEXT,
                    self_id_desc TEXT,
                    trajectory_json TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
            """)
            conn.execute("""
                INSERT INTO encounters VALUES (
                    'legacy_enc_001', 'AA:11:22:33:44:55', '1596E1LEGACY12345678',
                    1725800000.0, '2026-09-08T10:00:00Z', 1725800010.0, '2026-09-08T10:00:10Z',
                    10.0, 5, 'bt5', '37', -70, -60, -65.0, 100.0, 150.0, 8.0,
                    NULL, NULL, NULL, NULL, NULL, '[]', 0
                );
            """)
            conn.commit()

        # Initialize tracker on legacy DB -> triggers non-destructive migration and backfill
        tracker = EncounterTracker(db_path=self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM encounters WHERE encounter_id = 'legacy_enc_001'").fetchone()
            self.assertEqual(row["drone_make"], "Autel Robotics")
            self.assertIn("EVO II", row["drone_model"])


if __name__ == "__main__":
    unittest.main()
