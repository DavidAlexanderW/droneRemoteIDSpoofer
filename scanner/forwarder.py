#!/usr/bin/env python3
"""
Tactical Drone Remote ID - Distributed Edge Stream Forwarder
Implements a 3-Tier Reliable Forwarding Pipeline:
  Tier 1: Live WebSocket streaming with zero continuous disk writes (happy path)
  Tier 2: In-memory RAM buffer for transient disconnections (< 5-10 min)
  Tier 3: Spill-over disk spooling for extended outages (> RAM capacity)

On reconnect/startup:
  Executes high-speed catch-up burst across spool and historical logs,
  waits for Hub commit ACKs, and automatically unlinks/purges synced files.
"""

import asyncio
import glob
import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger("DroneRIDForwarder")


class CentralStreamForwarder:
    """
    Non-blocking background worker that connects to central_hub.py,
    executes historical catch-up sync, buffers in RAM / disk during outages,
    and streams real-time packets to the central ingestion hub.
    """

    def __init__(
        self,
        hub_ws_url: str,
        node_id: str,
        node_meta: Optional[Dict[str, Any]] = None,
        spool_dir: str = "spool",
        backlog_paths: Optional[List[str]] = None,
        max_ram_queue: int = 10000,
        batch_size: int = 250,
        quiet: bool = False,
    ):
        self.hub_ws_url = hub_ws_url.strip()
        self.node_id = node_id.strip()
        self.node_meta = node_meta or {}
        self.spool_dir = os.path.abspath(spool_dir)
        self.backlog_paths = backlog_paths or []
        self.max_ram_queue = max(1, max_ram_queue)
        self.batch_size = max(10, batch_size)
        self.quiet = quiet

        self.live_queue: queue.Queue = queue.Queue(maxsize=self.max_ram_queue)
        self.running = False
        self.connected = False
        self.last_connected_time: Optional[float] = None
        self.stats = {
            "packets_enqueued": 0,
            "packets_streamed_live": 0,
            "packets_synced_backlog": 0,
            "spool_files_written": 0,
            "spool_files_purged": 0,
            "connection_attempts": 0,
            "last_error": None,
        }
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None

        os.makedirs(self.spool_dir, exist_ok=True)

    def start(self):
        """Starts the background event loop and connection worker."""
        if self.running:
            return
        if websockets is None:
            logger.error("[-] 'websockets' library is required for CentralStreamForwarder. Install with: pip install websockets")
            return

        self.running = True
        self.thread = threading.Thread(target=self._thread_entry, name="ForwarderWorker", daemon=True)
        self.thread.start()
        if not self.quiet:
            logger.info(f"[*] CentralStreamForwarder started for node '{self.node_id}' -> {self.hub_ws_url}")

    def stop(self):
        """Gracefully stops the worker, flushing remaining in-memory packets to disk if disconnected."""
        if not self.running:
            return
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)

        # Flush any remaining items in RAM queue to a spill-over spool file
        self._flush_ram_to_spool_on_shutdown()

    def enqueue_packet(self, packet_event: Dict[str, Any]):
        """
        Enqueues a captured packet event. Non-blocking and thread-safe.
        Called directly by the sniffer / telemetry logger worker.
        """
        if not self.running:
            return

        envelope = {
            "version": "1.0",
            "node_id": self.node_id,
            "node_meta": self.node_meta,
            **packet_event,
        }

        with self.lock:
            self.stats["packets_enqueued"] += 1

        try:
            self.live_queue.put_nowait(envelope)
        except queue.Full:
            # RAM queue full (Tier 2 limit reached) -> Spill over a batch to disk (Tier 3)
            self._spill_overflow_to_disk()
            try:
                self.live_queue.put_nowait(envelope)
            except queue.Full:
                # Direct disk append if still full
                self._write_single_to_spool(envelope)

    def _spill_overflow_to_disk(self, chunk_size: int = 500):
        """Pulls chunk_size packets from RAM queue and writes them to a timestamped disk spool file."""
        spill_items = []
        for _ in range(min(chunk_size, self.live_queue.qsize())):
            try:
                spill_items.append(self.live_queue.get_nowait())
            except queue.Empty:
                break

        if spill_items:
            self._write_batch_to_spool(spill_items)

    def _write_batch_to_spool(self, items: List[Dict[str, Any]]):
        """Writes a batch of packets to a new temporary spool file."""
        if not items:
            return
        ts = int(time.time())
        u_id = uuid.uuid4().hex[:8]
        spool_filename = f"spool_{ts}_{u_id}.jsonl"
        spool_path = os.path.join(self.spool_dir, spool_filename)
        tmp_path = f"{spool_path}.tmp"

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for item in items:
                    f.write(json.dumps(item) + "\n")
            os.replace(tmp_path, spool_path)
            with self.lock:
                self.stats["spool_files_written"] += 1
            if not self.quiet:
                logger.warning(f"[!] RAM queue full: Spooled {len(items)} packets to disk: {spool_filename}")
        except Exception as e:
            logger.error(f"[-] Failed to write spill-over spool file: {e}")

    def _write_single_to_spool(self, item: Dict[str, Any]):
        """Appends a single overflow packet to emergency spool."""
        ts = int(time.time())
        spool_path = os.path.join(self.spool_dir, f"spool_overflow_{ts}.jsonl")
        try:
            with open(spool_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item) + "\n")
            with self.lock:
                self.stats["spool_files_written"] += 1
        except Exception as e:
            logger.error(f"[-] Emergency spool append failed: {e}")

    def _flush_ram_to_spool_on_shutdown(self):
        """Flushes any remaining in-memory packets to disk when shutting down."""
        remaining = []
        while not self.live_queue.empty():
            try:
                remaining.append(self.live_queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            self._write_batch_to_spool(remaining)
            if not self.quiet:
                logger.info(f"[*] Flushed {len(remaining)} remaining in-memory packets to disk spool on shutdown.")

    def _thread_entry(self):
        """Background thread running asyncio event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connection_manager())
        finally:
            loop.close()

    async def _connection_manager(self):
        """Manages WebSocket connection, handshake, catch-up bursts, and live streaming with auto-reconnect."""
        retry_delay = 2.0
        max_retry_delay = 30.0

        while self.running:
            with self.lock:
                self.stats["connection_attempts"] += 1
            ws_url = f"{self.hub_ws_url}?node_id={self.node_id}" if "?" not in self.hub_ws_url else f"{self.hub_ws_url}&node_id={self.node_id}"

            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self.connected = True
                    self.last_connected_time = time.time()
                    retry_delay = 2.0  # Reset backoff on success
                    if not self.quiet:
                        logger.info(f"[+] Connected to Central Ingestion Hub: {self.hub_ws_url}")

                    # 1. Handshake Phase
                    handshake_payload = {
                        "type": "handshake",
                        "node_id": self.node_id,
                        "node_meta": self.node_meta,
                        "timestamp_epoch": time.time(),
                    }
                    await ws.send(json.dumps(handshake_payload))

                    raw_ack = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    ack = json.loads(raw_ack)
                    last_synced_epoch = float(ack.get("last_synced_epoch", 0.0))
                    if not self.quiet:
                        logger.info(f"[+] Handshake complete with Hub. Node '{self.node_id}' sync watermark: {last_synced_epoch:.2f}")

                    # 2. Historical & Spool Catch-Up Phase (Drains spool files & backlog)
                    await self._sync_all_spool_and_backlog(ws, last_synced_epoch)

                    # 3. Live Streaming Phase
                    while self.running:
                        try:
                            # Non-blocking get with short sleep in asyncio
                            try:
                                pkt = self.live_queue.get_nowait()
                            except queue.Empty:
                                await asyncio.sleep(0.02)
                                continue

                            # Send live packet envelope
                            await ws.send(json.dumps({"type": "packet", **pkt}))
                            with self.lock:
                                self.stats["packets_streamed_live"] += 1

                        except websockets.ConnectionClosed:
                            break

            except Exception as e:
                self.connected = False
                with self.lock:
                    self.stats["last_error"] = str(e)
                if not self.quiet and self.running:
                    logger.debug(f"[!] Forwarder connection to Hub failed ({e}). Retrying in {retry_delay:.1f}s...")

                # Wait with exponential backoff before reconnecting
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)

    async def _sync_all_spool_and_backlog(self, ws, last_synced_epoch: float):
        """
        Discovers all pending spool files and historical JSONL logs,
        streams un-synced packets in batches, waits for Hub commit ACKs,
        and safely unlinks/purges the files once acknowledged.
        """
        candidate_files = []

        # 1. Collect all spool files (*.jsonl in spool_dir)
        if os.path.isdir(self.spool_dir):
            for f in sorted(os.listdir(self.spool_dir)):
                if f.endswith(".jsonl") and not f.endswith(".tmp"):
                    candidate_files.append(os.path.join(self.spool_dir, f))

        # 2. Collect any backlog glob paths
        for pat in self.backlog_paths:
            for p in sorted(glob.glob(pat)):
                if os.path.isfile(p) and p not in candidate_files:
                    candidate_files.append(p)

        if not candidate_files:
            return

        if not self.quiet:
            logger.info(f"[*] Catch-up sync found {len(candidate_files)} file(s) to inspect/synchronize.")

        for file_path in candidate_files:
            if not self.running:
                break
            try:
                await self._sync_single_file(ws, file_path, last_synced_epoch)
            except Exception as e:
                logger.error(f"[-] Error synchronizing file {file_path}: {e}")
                break  # If connection drops during batch sync, break to reconnect

    async def _sync_single_file(self, ws, file_path: str, last_synced_epoch: float):
        """Streams packets from a single file in batches and deletes the file upon Hub commit confirmation."""
        if not os.path.exists(file_path):
            return

        batch_items = []
        synced_count = 0
        file_basename = os.path.basename(file_path)

        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str or not line_str.startswith("{"):
                    continue
                try:
                    pkt = json.loads(line_str)
                    ts = pkt.get("timestamp", pkt.get("timestamp_epoch", 0.0))
                    if ts > last_synced_epoch:
                        envelope = {
                            "version": "1.0",
                            "node_id": self.node_id,
                            "node_meta": self.node_meta,
                            **pkt,
                        }
                        batch_items.append(envelope)

                        if len(batch_items) >= self.batch_size:
                            await self._send_and_ack_batch(ws, file_basename, batch_items)
                            synced_count += len(batch_items)
                            batch_items = []
                except Exception:
                    continue

        if batch_items:
            await self._send_and_ack_batch(ws, file_basename, batch_items)
            synced_count += len(batch_items)

        # Batch sync completed successfully -> delete / purge spool file
        try:
            os.remove(file_path)
            with self.lock:
                self.stats["spool_files_purged"] += 1
                self.stats["packets_synced_backlog"] += synced_count
            if not self.quiet:
                logger.info(f"[+] Catch-up sync complete: {file_basename} ({synced_count} pkts). Purged from disk.")
        except Exception as e:
            logger.warning(f"[!] Could not remove synchronized file {file_path}: {e}")

    async def _send_and_ack_batch(self, ws, file_basename: str, items: List[Dict[str, Any]]):
        """Sends a batch of packets and awaits explicit Hub commit ACK."""
        batch_id = f"batch_{file_basename}_{uuid.uuid4().hex[:6]}"
        payload = {
            "type": "batch",
            "batch_id": batch_id,
            "node_id": self.node_id,
            "node_meta": self.node_meta,
            "items": items,
        }
        await ws.send(json.dumps(payload))

        # Wait for batch_ack from Central Hub
        raw_ack = await asyncio.wait_for(ws.recv(), timeout=30.0)
        ack = json.loads(raw_ack)
        if ack.get("type") != "batch_ack" or ack.get("batch_id") != batch_id or ack.get("status") != "committed":
            raise RuntimeError(f"Invalid batch ACK from Hub: {raw_ack}")
