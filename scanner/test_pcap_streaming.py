#!/usr/bin/env python3
"""
test_pcap_streaming.py - Unit & Integration Tests for Concurrent Binary PCAP Streaming

Verifies:
1. BinaryPcapStreamer queue buffering, framing, batch draining, and RAM backpressure.
2. Central Ingestion Hub binary WebSocket endpoint: /stream/pcap/{node_id}/{media}.
3. Concurrent execution: simultaneous JSON telemetry on /stream/node and binary PCAP on /stream/pcap.
4. Correctness of generated PCAP files (libpcap 2.4 headers, DLT 127 for Wi-Fi, DLT 272 for BLE).
"""

import asyncio
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

# Ensure repo root and scanner dir in sys.path
_scanner_dir = os.path.abspath(os.path.dirname(__file__))
_repo_root = os.path.abspath(os.path.join(_scanner_dir, ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
if _scanner_dir not in sys.path:
    sys.path.insert(0, _scanner_dir)

from fastapi.testclient import TestClient

from scanner.central_hub import CentralIngestionHub, create_central_hub_app
from scanner.pcap_logger import (
    DailyNodePcapLogger,
    DLT_IEEE802_11_RADIO,
    DLT_NORDIC_BLE,
    PCAP_GLOBAL_HEADER_LEN,
    PCAP_RECORD_HEADER_LEN,
    build_pcap_record_header,
)
from scanner.pcap_streamer import BinaryPcapStreamer, normalize_ws_base_url


class TestPcapStreaming(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_pcap_streaming_")
        self.db_path = os.path.join(self.test_dir, "test_rid_central.db")
        self.log_dir = os.path.join(self.test_dir, "central_logs")
        self.pcap_dir = os.path.join(self.test_dir, "central_logs", "pcaps")

        self.hub = CentralIngestionHub(
            db_path=self.db_path,
            log_dir=self.log_dir,
            pcap_dir=self.pcap_dir,
            timeout_s=300.0,
            log_pcap=True,
        )
        self.app = create_central_hub_app(self.hub)
        self.client = TestClient(self.app)

    def tearDown(self):
        try:
            self.hub.close()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_url_normalization(self):
        """Verifies normalization of various hub URLs to clean base WebSocket URLs."""
        self.assertEqual(normalize_ws_base_url("http://192.168.1.50:8000"), "ws://192.168.1.50:8000")
        self.assertEqual(normalize_ws_base_url("https://hub.example.com"), "wss://hub.example.com")
        self.assertEqual(normalize_ws_base_url("ws://127.0.0.1:8000/stream/node"), "ws://127.0.0.1:8000")
        self.assertEqual(normalize_ws_base_url("127.0.0.1:8000"), "ws://127.0.0.1:8000")

    def test_binary_pcap_streamer_buffering_and_backpressure(self):
        """Verifies in-memory queue management and bounded RAM buffer drop behavior."""
        streamer = BinaryPcapStreamer(
            hub_url="ws://127.0.0.1:8000",
            node_id="test_node_buffer",
            max_buffer_bytes=1000,  # Very small buffer to test backpressure drop
            quiet=True,
        )

        streamer.running = True  # Enable enqueueing without starting async loop

        # Enqueue 10 Wi-Fi frames of 100 bytes each (each record is 16 + 100 = 116 bytes)
        fake_wifi_frame = b"\x00" * 100
        t0 = 1788800000.0
        for i in range(15):
            streamer.enqueue_wifi(fake_wifi_frame, ts=t0 + i)

        stats = streamer.get_stats()
        self.assertEqual(stats["wifi_packets_enqueued"], 15)
        # Should have dropped some packets because 15 * 116 = 1740 > 1000 bytes
        self.assertGreater(stats["wifi_packets_dropped"], 0)
        self.assertLessEqual(stats["wifi_buffer_bytes"], 1000)

        # Enqueue raw BLE chunks
        fake_ble_record = build_pcap_record_header(t0, 50) + (b"\xAA" * 50)
        for i in range(25):
            streamer.enqueue_ble_raw(fake_ble_record)

        stats = streamer.get_stats()
        self.assertEqual(stats["ble_packets_enqueued"], 25)
        self.assertGreater(stats["ble_packets_dropped"], 0)
        self.assertLessEqual(stats["ble_buffer_bytes"], 1000)

        streamer.running = False

    def test_hub_binary_pcap_websocket_endpoint(self):
        """Tests the hub /stream/pcap/{node_id}/{media} binary WebSocket route directly."""
        node_id = "node_pcap_test"
        ts = 1788805000.0  # Sep 7, 2026 UTC

        # Build 3 valid Wi-Fi PCAP records
        wifi_records = []
        for i in range(3):
            payload = b"\x80\x00\x00\x00\xFF\xFF\xFF\xFF\xFF\xFF" + bytes([i] * 20)
            rec_hdr = build_pcap_record_header(ts + (i * 0.1), len(payload))
            wifi_records.append(rec_hdr + payload)
        wifi_chunk = b"".join(wifi_records)

        # Connect to Wi-Fi binary PCAP endpoint
        with self.client.websocket_connect(f"/stream/pcap/{node_id}/wifi") as ws:
            ws.send_bytes(wifi_chunk)
            time.sleep(0.1)

        # Build 2 valid BLE PCAP records
        ble_records = []
        for i in range(2):
            payload = b"\x00\x10\x02\x00\x06\x0A\x01\x25\x3C\x01\x00\x00\x00\x00" + bytes([i] * 15)
            rec_hdr = build_pcap_record_header(ts + (i * 0.2), len(payload))
            ble_records.append(rec_hdr + payload)
        ble_chunk = b"".join(ble_records)

        # Connect to BLE binary PCAP endpoint
        with self.client.websocket_connect(f"/stream/pcap/{node_id}/ble") as ws:
            ws.send_bytes(ble_chunk)
            time.sleep(0.1)

        # Verify generated files on Hub
        pcap_files = os.listdir(self.pcap_dir)
        wifi_files = [f for f in pcap_files if f.startswith(f"{node_id}_wifi")]
        ble_files = [f for f in pcap_files if f.startswith(f"{node_id}_ble")]

        self.assertEqual(len(wifi_files), 1, f"Expected 1 Wi-Fi PCAP file, found {pcap_files}")
        self.assertEqual(len(ble_files), 1, f"Expected 1 BLE PCAP file, found {pcap_files}")

        # Verify Wi-Fi PCAP header and packets
        wifi_path = os.path.join(self.pcap_dir, wifi_files[0])
        with open(wifi_path, "rb") as f:
            ghdr = f.read(PCAP_GLOBAL_HEADER_LEN)
            magic, vmaj, vmin, tz, sig, snaplen, dlt = struct.unpack("<IHHiIII", ghdr)
            self.assertEqual(magic, 0xa1b2c3d4)
            self.assertEqual(dlt, DLT_IEEE802_11_RADIO)

            # Read back 3 packets
            for i in range(3):
                rhdr = f.read(PCAP_RECORD_HEADER_LEN)
                self.assertEqual(len(rhdr), PCAP_RECORD_HEADER_LEN)
                s_sec, s_usec, incl_len, orig_len = struct.unpack("<IIII", rhdr)
                self.assertEqual(incl_len, 30)
                pkt = f.read(incl_len)
                self.assertEqual(len(pkt), 30)

        # Verify BLE PCAP header and packets
        ble_path = os.path.join(self.pcap_dir, ble_files[0])
        with open(ble_path, "rb") as f:
            ghdr = f.read(PCAP_GLOBAL_HEADER_LEN)
            magic, vmaj, vmin, tz, sig, snaplen, dlt = struct.unpack("<IHHiIII", ghdr)
            self.assertEqual(magic, 0xa1b2c3d4)
            self.assertEqual(dlt, DLT_NORDIC_BLE)

            # Read back 2 packets
            for i in range(2):
                rhdr = f.read(PCAP_RECORD_HEADER_LEN)
                self.assertEqual(len(rhdr), PCAP_RECORD_HEADER_LEN)
                s_sec, s_usec, incl_len, orig_len = struct.unpack("<IIII", rhdr)
                self.assertEqual(incl_len, 29)
                pkt = f.read(incl_len)
                self.assertEqual(len(pkt), 29)

    def test_concurrent_dual_pipeline_streaming(self):
        """
        Verifies dual concurrent pipeline operation:
        Pipeline 1: Pure JSON telemetry on /stream/node (including ASTM BLE advertisement)
        Pipeline 2: Full binary PCAP stream on /stream/pcap/{node_id}/{media}
        """
        node_id = "sensor_concurrent_01"
        ts = 1788806000.0

        # 1. Establish live JSON telemetry WebSocket stream on /stream/node
        with self.client.websocket_connect(f"/stream/node?node_id={node_id}") as json_ws:
            # Handshake
            json_ws.send_text('{"type": "handshake", "node_id": "' + node_id + '", "node_meta": {"name": "Lab Node"}}')
            ack = json_ws.receive_json()
            self.assertEqual(ack["status"], "ready")

            # Concurrently open Wi-Fi and BLE binary PCAP streams
            with self.client.websocket_connect(f"/stream/pcap/{node_id}/wifi") as wifi_ws:
                with self.client.websocket_connect(f"/stream/pcap/{node_id}/ble") as ble_ws:

                    # Send ASTM BLE Remote ID advertisement via Pipeline 1 (JSON)
                    astm_ble_event = {
                        "type": "packet",
                        "node_id": node_id,
                        "transport": "bt4",
                        "mac": "66:77:88:99:AA:BB",
                        "serial_number": "CONCURRENT_BLE_DRONE",
                        "rssi_dbm": -55,
                        "timestamp": ts,
                        "counter": 1,
                        "messages": [{"type": "Basic ID", "id_type": "Serial", "serial": "CONCURRENT_BLE_DRONE"}],
                    }
                    json_ws.send_text(json.dumps_str(astm_ble_event) if hasattr(json, "dumps_str") else __import__("json").dumps(astm_ble_event))

                    # Simultaneously send raw ambient frames via Pipeline 2 (Binary PCAP)
                    raw_wifi = b"\x80\x00\x00\x00\xFF\xFF\xFF\xFF\xFF\xFF" + (b"\x11" * 40)
                    wifi_rec = build_pcap_record_header(ts, len(raw_wifi)) + raw_wifi
                    wifi_ws.send_bytes(wifi_rec)

                    raw_ble = b"\x00\x10\x02\x00\x06\x0A\x01\x25\x3C\x01\x00\x00\x00\x00" + (b"\x22" * 35)
                    ble_rec = build_pcap_record_header(ts, len(raw_ble)) + raw_ble
                    ble_ws.send_bytes(ble_rec)

                    time.sleep(0.15)

            # Send a heartbeat on JSON stream
            json_ws.send_text('{"type": "heartbeat", "node_id": "' + node_id + '"}')
            hb_ack = json_ws.receive_json()
            self.assertEqual(hb_ack["status"], "ok")

        # Verify Pipeline 1 (JSON telemetry): encounter in DB and JSONL record
        active_encs = self.hub.encounter_tracker.active_encounters
        self.assertIn("66:77:88:99:AA:BB", active_encs)
        self.assertEqual(active_encs["66:77:88:99:AA:BB"]["serial_number"], "CONCURRENT_BLE_DRONE")

        jsonl_files = [f for f in os.listdir(self.log_dir) if f.endswith(".jsonl")]
        self.assertEqual(len(jsonl_files), 1)

        # Verify Pipeline 2 (Binary PCAP): valid daily PCAP files
        pcap_files = os.listdir(self.pcap_dir)
        wifi_files = [f for f in pcap_files if f.startswith(f"{node_id}_wifi")]
        ble_files = [f for f in pcap_files if f.startswith(f"{node_id}_ble")]
        self.assertEqual(len(wifi_files), 1)
        self.assertEqual(len(ble_files), 1)

        # Verify PCAP file sizes (> 24-byte global header + record)
        wifi_sz = os.path.getsize(os.path.join(self.pcap_dir, wifi_files[0]))
        ble_sz = os.path.getsize(os.path.join(self.pcap_dir, ble_files[0]))
        self.assertEqual(wifi_sz, 24 + 16 + len(raw_wifi))
        self.assertEqual(ble_sz, 24 + 16 + len(raw_ble))


if __name__ == "__main__":
    unittest.main()
