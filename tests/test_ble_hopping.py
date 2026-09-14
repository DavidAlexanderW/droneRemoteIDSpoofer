#!/usr/bin/env python3
"""
Unit tests for Bluetooth 4 / 5 Mode Hopping in Drone Remote ID Scanner.
"""

import os
import sys
import unittest

# Ensure repo root is in sys.path
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scanner.scanner_config import DEFAULT_SCANNER_CONFIG, load_scanner_config
from scanner.sniffers.nrf_bt_sniffer_json import BleModeHopperController, extract_remote_id_info


class TestBleHopping(unittest.TestCase):
    def test_default_config_keys(self):
        self.assertIn("ble_mode", DEFAULT_SCANNER_CONFIG)
        self.assertIn("ble_bt5_dwell_s", DEFAULT_SCANNER_CONFIG)
        self.assertIn("ble_bt4_dwell_s", DEFAULT_SCANNER_CONFIG)
        self.assertEqual(DEFAULT_SCANNER_CONFIG["ble_mode"], "hop")
        self.assertEqual(DEFAULT_SCANNER_CONFIG["ble_bt5_dwell_s"], 5.0)
        self.assertEqual(DEFAULT_SCANNER_CONFIG["ble_bt4_dwell_s"], 1.0)

    def test_controller_cmd_generation(self):
        ctrl = BleModeHopperController(
            nrf_port="/dev/ttyACM0",
            pcap_path="/tmp/test.fifo",
            mode="hop",
            bt5_dwell_s=5.0,
            bt4_dwell_s=1.0,
            filter_mac="11:22:33:44:55:66"
        )
        
        # Test BT5 command
        cmd_bt5 = ctrl._get_cmd_for_mode("bt5")
        self.assertIn("--scan-follow-aux", cmd_bt5)
        self.assertIn("--scan-follow-aux-chain", cmd_bt5)
        self.assertIn("--coded", cmd_bt5)
        self.assertIn("--follow", cmd_bt5)
        self.assertIn("11:22:33:44:55:66", cmd_bt5)
        self.assertNotIn("--only-legacy_advertising", cmd_bt5)

        # Test BT4 command
        cmd_bt4 = ctrl._get_cmd_for_mode("bt4")
        self.assertIn("--only-legacy_advertising", cmd_bt4)
        self.assertNotIn("--coded", cmd_bt4)
        self.assertNotIn("--scan-follow-aux", cmd_bt4)
        self.assertIn("--follow", cmd_bt4)
        self.assertIn("11:22:33:44:55:66", cmd_bt4)


if __name__ == "__main__":
    unittest.main()
