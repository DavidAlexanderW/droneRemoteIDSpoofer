#!/usr/bin/env python3
"""
Unit and integration tests for scanner/forwarder.py (CentralStreamForwarder):
1. In-memory queueing and capacity limits
2. Spill-over disk spooling on RAM queue overflow
3. Shutdown flush of remaining RAM buffer to disk
4. Catch-up sync across spool files and file purge upon ACK
"""

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from scanner.forwarder import CentralStreamForwarder


class TestCentralStreamForwarder(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_forwarder_")
        self.spool_dir = os.path.join(self.test_dir, "spool")
        os.makedirs(self.spool_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_enqueue_and_ram_buffering(self):
        forwarder = CentralStreamForwarder(
            hub_ws_url="ws://127.0.0.1:9999/stream/node",
            node_id="test-node-01",
            spool_dir=self.spool_dir,
            max_ram_queue=10,
            quiet=True,
        )
        forwarder.running = True

        # Enqueue 5 packets (within RAM limit)
        for i in range(5):
            forwarder.enqueue_packet({
                "mac": f"AA:BB:CC:00:00:0{i}",
                "timestamp": 1788800000.0 + i,
                "rssi_dbm": -60 - i,
            })

        self.assertEqual(forwarder.live_queue.qsize(), 5)
        self.assertEqual(forwarder.stats["packets_enqueued"], 5)
        # Verify no disk spool files were created (0 disk writes on normal queue)
        spool_files = os.listdir(self.spool_dir)
        self.assertEqual(len(spool_files), 0)

    def test_spill_over_disk_spooling_on_ram_overflow(self):
        max_ram = 5
        forwarder = CentralStreamForwarder(
            hub_ws_url="ws://127.0.0.1:9999/stream/node",
            node_id="test-node-01",
            spool_dir=self.spool_dir,
            max_ram_queue=max_ram,
            quiet=True,
        )
        forwarder.running = True

        # Enqueue 15 packets -> exceeds max_ram=5 and triggers spill-over to disk
        for i in range(15):
            forwarder.enqueue_packet({
                "mac": f"AA:BB:CC:00:00:{i:02d}",
                "timestamp": 1788800000.0 + i,
                "rssi_dbm": -60 - i,
            })

        self.assertGreater(forwarder.stats["spool_files_written"], 0)
        spool_files = [f for f in os.listdir(self.spool_dir) if f.endswith(".jsonl")]
        self.assertGreater(len(spool_files), 0)

        # Verify content of spooled file
        spool_path = os.path.join(self.spool_dir, spool_files[0])
        with open(spool_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertGreater(len(lines), 0)
        first_item = json.loads(lines[0])
        self.assertEqual(first_item["node_id"], "test-node-01")
        self.assertIn("version", first_item)

    def test_shutdown_flush_to_spool(self):
        forwarder = CentralStreamForwarder(
            hub_ws_url="ws://127.0.0.1:9999/stream/node",
            node_id="test-node-02",
            spool_dir=self.spool_dir,
            max_ram_queue=100,
            quiet=True,
        )
        forwarder.running = True

        # Enqueue 3 items
        for i in range(3):
            forwarder.enqueue_packet({
                "mac": f"11:22:33:44:55:0{i}",
                "timestamp": 1788800000.0 + i,
            })
        self.assertEqual(forwarder.live_queue.qsize(), 3)

        # Stop forwarder -> should flush remaining items in RAM to a spool file
        forwarder.stop()

        spool_files = [f for f in os.listdir(self.spool_dir) if f.endswith(".jsonl")]
        self.assertEqual(len(spool_files), 1)

        spool_path = os.path.join(self.spool_dir, spool_files[0])
        with open(spool_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 3)

    def test_catch_up_sync_and_file_purge(self):
        # Create 2 pending spool files
        spool1 = os.path.join(self.spool_dir, "spool_100_a.jsonl")
        spool2 = os.path.join(self.spool_dir, "spool_200_b.jsonl")

        with open(spool1, "w", encoding="utf-8") as f:
            f.write(json.dumps({"mac": "AA:BB:CC:11:22:33", "timestamp": 1788810010.0}) + "\n")
            f.write(json.dumps({"mac": "AA:BB:CC:11:22:33", "timestamp": 1788810020.0}) + "\n")

        with open(spool2, "w", encoding="utf-8") as f:
            f.write(json.dumps({"mac": "DD:EE:FF:11:22:33", "timestamp": 1788810030.0}) + "\n")

        self.assertEqual(len(os.listdir(self.spool_dir)), 2)

        forwarder = CentralStreamForwarder(
            hub_ws_url="ws://127.0.0.1:9999/stream/node",
            node_id="test-node-purge",
            spool_dir=self.spool_dir,
            max_ram_queue=100,
            quiet=True,
        )
        forwarder.running = True

        # Mock WebSocket client object with send and recv
        class MockWebSocket:
            def __init__(self):
                self.sent_messages = []

            async def send(self, data):
                self.sent_messages.append(json.loads(data))

            async def recv(self):
                last_sent = self.sent_messages[-1]
                batch_id = last_sent.get("batch_id", "test_batch")
                return json.dumps({
                    "type": "batch_ack",
                    "batch_id": batch_id,
                    "status": "committed",
                })

        mock_ws = MockWebSocket()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Run catch-up sync
        loop.run_until_complete(forwarder._sync_all_spool_and_backlog(mock_ws, last_synced_epoch=0.0))
        loop.close()

        # Verify all spool files were sent and purged from disk
        remaining_files = os.listdir(self.spool_dir)
        self.assertEqual(len(remaining_files), 0)
        self.assertEqual(forwarder.stats["spool_files_purged"], 2)
        self.assertEqual(forwarder.stats["packets_synced_backlog"], 3)


if __name__ == "__main__":
    unittest.main()
