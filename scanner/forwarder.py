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
from datetime import datetime, timezone
import glob
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

# Ensure repository root in sys.path for parser imports
_scanner_dir = os.path.abspath(os.path.dirname(__file__))
_repo_root = os.path.abspath(os.path.join(_scanner_dir, ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
if _scanner_dir not in sys.path:
    sys.path.insert(0, _scanner_dir)

try:
    from scanner.parser import decode_astm_message, parse_astm_payload
except ImportError:
    try:
        from drone_rid_spoofer.parser import decode_astm_message, parse_astm_payload
    except ImportError:
        try:
            from parser import decode_astm_message, parse_astm_payload
        except ImportError:
            decode_astm_message = None
            parse_astm_payload = None

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
        force_resync: bool = False,
        quiet: bool = False,
    ):
        self.hub_ws_url = hub_ws_url.strip()
        self.node_id = node_id.strip()
        self.node_meta = node_meta or {}
        self.spool_dir = os.path.abspath(spool_dir)
        self.backlog_paths = backlog_paths or []
        self.max_ram_queue = max(1, max_ram_queue)
        self.batch_size = max(10, batch_size)
        self.force_resync = force_resync
        self.quiet = quiet

        self.live_queue: queue.Queue = queue.Queue(maxsize=self.max_ram_queue)
        self.running = False
        self.connected = False
        self.catchup_complete = threading.Event()
        self.last_connected_time: Optional[float] = None
        self.last_hub_ack_time: Optional[float] = None
        self.hub_ack_timeout_s = 45.0  # Reconnect if Hub stops ACKing heartbeats for this long
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
                    logger.info(f"[+] Handshake complete with Hub. Node '{self.node_id}' sync watermark: {last_synced_epoch:.2f}")

                    # 2. Historical & Spool Catch-Up Phase (Drains spool files & backlog)
                    await self._sync_all_spool_and_backlog(ws, last_synced_epoch)

                    # 3. Live Streaming Phase
                    # Spawn a concurrent reader task to drain incoming messages
                    # (heartbeat_acks, future commands) and prevent buffer overflow.
                    self.last_hub_ack_time = time.time()
                    reader_task = asyncio.create_task(self._hub_receiver_loop(ws))

                    last_heartbeat_time = time.time()
                    try:
                        while self.running:
                            try:
                                now = time.time()
                                if now - last_heartbeat_time >= 15.0:
                                    last_heartbeat_time = now
                                    heartbeat_payload = {
                                        "type": "heartbeat",
                                        "node_id": self.node_id,
                                        "node_meta": self.node_meta,
                                        "timestamp_epoch": now,
                                    }
                                    await ws.send(json.dumps(heartbeat_payload))

                                # Detect zombie Hub (application alive but not processing)
                                if self.last_hub_ack_time and (now - self.last_hub_ack_time > self.hub_ack_timeout_s):
                                    logger.warning(f"[!] Hub application not acknowledging heartbeats for {now - self.last_hub_ack_time:.0f}s (zombie server). Reconnecting...")
                                    break

                                # Non-blocking get with short sleep in asyncio
                                try:
                                    pkt = self.live_queue.get_nowait()
                                except queue.Empty:
                                    await asyncio.sleep(0.05)
                                    continue

                                # Send live packet envelope
                                await ws.send(json.dumps({"type": "packet", **pkt}))
                                with self.lock:
                                    self.stats["packets_streamed_live"] += 1

                            except websockets.ConnectionClosed:
                                break
                    finally:
                        reader_task.cancel()
                        try:
                            await reader_task
                        except asyncio.CancelledError:
                            pass

            except Exception as e:
                self.connected = False
                with self.lock:
                    self.stats["last_error"] = str(e)
                if self.running:
                    logger.warning(f"[!] Forwarder connection to Hub failed ({e}). Retrying in {retry_delay:.1f}s...")

                # Wait with exponential backoff before reconnecting
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)

    async def _hub_receiver_loop(self, ws):
        """Concurrent reader task that drains incoming Hub messages (heartbeat_acks, future commands)
        to prevent the receive buffer from filling up and stalling protocol-level ping/pong frames."""
        try:
            async for msg_str in ws:
                try:
                    msg = json.loads(msg_str)
                    msg_type = msg.get("type", "")
                    if msg_type == "heartbeat_ack":
                        self.last_hub_ack_time = time.time()
                    # Future: handle Hub-to-Node commands here (e.g., channel change, reboot)
                except (json.JSONDecodeError, Exception):
                    pass
        except websockets.ConnectionClosed:
            pass

    async def _sync_all_spool_and_backlog(self, ws, last_synced_epoch: float):
        """
        Discovers all pending spool files and historical JSONL logs,
        streams un-synced packets in batches, waits for Hub commit ACKs,
        and safely unlinks/purges the temporary spool files once acknowledged.
        """
        candidate_files = []

        # 1. Collect all spool files (*.jsonl in spool_dir)
        if os.path.isdir(self.spool_dir):
            for f in sorted(os.listdir(self.spool_dir)):
                if f.endswith(".jsonl") and not f.endswith(".tmp"):
                    candidate_files.append(os.path.join(self.spool_dir, f))

        # 2. Collect backlog glob paths
        search_paths = list(self.backlog_paths)

        for pat in search_paths:
            for p in sorted(glob.glob(pat)):
                abs_p = os.path.abspath(p)
                if os.path.isfile(abs_p) and abs_p not in candidate_files:
                    candidate_files.append(abs_p)

        if candidate_files:
            logger.info(f"[*] Catch-up sync found {len(candidate_files)} file(s) to inspect/synchronize: {[os.path.basename(p) for p in candidate_files]}")

        for file_path in candidate_files:
            if not self.running:
                break
            try:
                await self._sync_single_file(ws, file_path, last_synced_epoch)
            except Exception as e:
                logger.error(f"[-] Error synchronizing file {file_path}: {e}")
                break  # If connection drops during batch sync, break to reconnect

        self.catchup_complete.set()

    async def _sync_single_file(self, ws, file_path: str, last_synced_epoch: float):
        """Streams packets from a single file in batches and deletes only temporary spool files upon Hub commit confirmation."""
        if not os.path.exists(file_path):
            return

        is_spool = os.path.abspath(file_path).startswith(self.spool_dir)
        batch_items = []
        synced_count = 0
        total_lines = 0
        file_basename = os.path.basename(file_path)

        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str or not line_str.startswith("{"):
                    continue
                total_lines += 1
                try:
                    pkt = json.loads(line_str)
                    now_ts = time.time()
                    min_valid = 1700000000.0
                    max_valid = 2000000000.0  # May 18, 2033 UTC (catches uncalibrated hardware tick overflows like 2061)

                    ts = pkt.get("reception_timestamp") or pkt.get("timestamp") or pkt.get("timestamp_epoch")
                    if ts is None:
                        iso_str = pkt.get("timestamp_iso")
                        if iso_str:
                            try:
                                dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                                ts = dt.timestamp()
                            except Exception:
                                ts = None
                    if ts is None or ts < min_valid or ts > max_valid:
                        recovered = None
                        for m in (pkt.get("messages") or []):
                            sys_ep = m.get("system_timestamp_epoch")
                            if sys_ep and min_valid <= sys_ep <= max_valid:
                                recovered = float(sys_ep)
                                break
                        ts = recovered if recovered is not None else now_ts

                    # Spool files obey watermark; historical archive backlog files sync in full (unless older than watermark and not forced)
                    should_sync = False
                    if is_spool:
                        should_sync = (ts > last_synced_epoch) or (last_synced_epoch <= 0.0)
                    else:
                        should_sync = self.force_resync or (last_synced_epoch <= 0.0) or (ts > last_synced_epoch)

                    if should_sync:
                        # Normalize serial number key
                        if not pkt.get("serial_number") and pkt.get("serial"):
                            pkt["serial_number"] = pkt["serial"]

                        # If messages list is missing but raw messages_b64 is present, decode it
                        if not pkt.get("messages") and pkt.get("messages_b64"):
                            try:
                                import base64
                                raw_blocks = [base64.b64decode(b) for b in pkt["messages_b64"]]
                                if parse_astm_payload:
                                    parsed_msgs, _ = parse_astm_payload(b"".join(raw_blocks))
                                    pkt["messages"] = parsed_msgs
                                elif decode_astm_message:
                                    pkt["messages"] = [decode_astm_message(b) for b in raw_blocks if decode_astm_message(b)]
                            except Exception:
                                pass

                        envelope = {
                            "version": "1.0",
                            "node_id": self.node_id,
                            "node_meta": self.node_meta,
                            **pkt,
                            "timestamp": ts,
                            "reception_timestamp": ts,
                            "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
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

        # Batch sync completed successfully -> purge temporary spool files, preserve historical archive files
        if is_spool:
            try:
                os.remove(file_path)
                with self.lock:
                    self.stats["spool_files_purged"] += 1
                    self.stats["packets_synced_backlog"] += synced_count
                logger.info(f"[+] Catch-up sync complete: {file_basename} ({synced_count} pkts). Spool purged from disk.")
            except Exception as e:
                logger.warning(f"[!] Could not remove synchronized spool file {file_path}: {e}")
        else:
            with self.lock:
                self.stats["packets_synced_backlog"] += synced_count
            if synced_count > 0:
                logger.info(f"[+] Historical sync complete: {file_basename} ({synced_count} pkts). Archive preserved on disk.")
            elif total_lines > 0:
                logger.info(f"[*] Historical file {file_basename} is already synchronized with Hub watermark (0 new pkts).")

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


# ============================================================================
# CLI Entry Point & Standalone Backlog Upload Utility
# ============================================================================

def main():
    import argparse

    try:
        from scanner.scanner_config import load_scanner_config
    except ImportError:
        try:
            from scanner_config import load_scanner_config
        except ImportError:
            load_scanner_config = None

    cfg = load_scanner_config() if load_scanner_config else {}
    default_node_id = cfg.get("node_id", "sensor-node-01")
    default_hub_url = cfg.get("hub_ws_url", "ws://127.0.0.1:8000/stream/node")
    default_spool_dir = cfg.get("spool_dir", "spool")

    parser = argparse.ArgumentParser(
        description="Tactical Drone Remote ID - Distributed Stream Forwarder & Historical Data Upload Utility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--hub-url", default=default_hub_url, help="Central Ingestion Hub WebSocket URL (e.g. ws://hub-ip:8000/stream/node)")
    parser.add_argument("--node-id", default=default_node_id, help="Sensor node identifier")
    parser.add_argument("--files", "--backlog", "-f", nargs="*", default=None, help="Specific JSONL capture file(s) or glob patterns to upload")
    parser.add_argument("--scanner-config", default=None, help="Path to scanner_config.json")
    parser.add_argument("--spool-dir", default=default_spool_dir, help="Spool directory for temporary outage buffers")
    parser.add_argument("--batch-size", type=int, default=250, help="Number of packets per catch-up batch")
    parser.add_argument("--force", action="store_true", help="Force upload of all historical records regardless of watermark")
    parser.add_argument("--sync-only", action="store_true", default=True, help="Exit cleanly after catch-up backlog synchronization finishes")
    parser.add_argument("--stream", dest="sync_only", action="store_false", help="Keep running after sync for live streaming")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose console logs")

    args = parser.parse_args()

    if args.scanner_config and load_scanner_config:
        try:
            cfg = load_scanner_config(args.scanner_config)
            if args.hub_url == default_hub_url:
                args.hub_url = cfg.get("hub_ws_url", default_hub_url)
            if args.node_id == default_node_id:
                args.node_id = cfg.get("node_id", default_node_id)
        except Exception as e:
            logger.warning(f"Failed to load scanner config '{args.scanner_config}': {e}")

    if args.files:
        backlog_files = list(args.files)
    else:
        backlog_files = [
            os.path.join(os.getcwd(), "rid_packets*.jsonl"),
            os.path.join(os.getcwd(), "capture*.jsonl"),
            os.path.join(_repo_root, "rid_packets*.jsonl"),
            os.path.join(_repo_root, "capture*.jsonl"),
            os.path.join(_scanner_dir, "rid_packets*.jsonl"),
        ]

    print("\n\033[1;32m📡 DRONE REMOTE ID DISTRIBUTED STREAM FORWARDER\033[0m")
    print(f"  • Central Hub URL: \033[1;36m{args.hub_url}\033[0m")
    print(f"  • Node Identifier: \033[1;35m{args.node_id}\033[0m")
    print(f"  • Spool Directory: \033[1;33m{os.path.abspath(args.spool_dir)}\033[0m")
    if backlog_files:
        print(f"  • Target Files   : \033[1;34m{backlog_files}\033[0m")
    else:
        print(f"  • Backlog Search : \033[1;34mAuto-detecting rid_packets_*.jsonl\033[0m")
    print(f"  • Force Re-sync  : \033[1;33m{'YES' if args.force else 'NO (Watermarked)'}\033[0m\n")

    forwarder = CentralStreamForwarder(
        hub_ws_url=args.hub_url,
        node_id=args.node_id,
        node_meta=cfg,
        spool_dir=args.spool_dir,
        backlog_paths=backlog_files,
        batch_size=args.batch_size,
        force_resync=args.force,
        quiet=args.quiet,
    )
    forwarder.start()

    if args.sync_only:
        print("[*] Waiting for historical catch-up sync to complete...")
        completed = forwarder.catchup_complete.wait(timeout=120.0)
        # Allow short grace period for background ACKs
        time.sleep(1.0)
        forwarder.stop()
        if completed:
            print(f"\n\033[1;32m[✓] Historical backlog upload finished successfully!\033[0m")
            print(f"    • Total Backlog Packets Synced: {forwarder.stats['packets_synced_backlog']}")
            print(f"    • Spool Files Purged:          {forwarder.stats['spool_files_purged']}\n")
        else:
            print("\n\033[1;31m[-] Catch-up sync timed out or was interrupted.\033[0m\n")
            sys.exit(1)
    else:
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[*] Stopping forwarder...")
            forwarder.stop()


if __name__ == "__main__":
    main()
