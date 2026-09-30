#!/usr/bin/env python3
"""
pcap_streamer.py - High-Performance Binary PCAP Streamer Client for Drone Remote ID

Concurrently streams raw unfiltered 802.11 Wi-Fi frames and BLE Link Layer packets
from edge sensor nodes to the Central Ingestion Hub over dedicated binary WebSockets:
  • Wi-Fi stream : ws://<hub>/stream/pcap/{node_id}/wifi
  • BLE stream   : ws://<hub>/stream/pcap/{node_id}/ble

Features:
- Standard libpcap 2.4 16-byte packet record framing.
- In-memory bounded ring buffer (50MB) with oldest-drop protection to protect edge SD/flash.
- High-throughput batching (32KB / 50ms) to reduce network syscalls.
- Autonomous reconnection with exponential backoff on network dropouts.
- Zero impact on real-time JSON telemetry streaming or ASTM BLE parsing.
"""

import asyncio
import logging
import os
import queue
import struct
import sys
import threading
import time
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("BinaryPcapStreamer")

try:
    import websockets
except ImportError:
    websockets = None

# PCAP record header constants
PCAP_RECORD_HEADER_LEN = 16


def build_pcap_record_header(ts: float, pkt_len: int) -> bytes:
    """Builds a standard 16-byte libpcap packet record header (little-endian)."""
    ts_sec = int(ts)
    ts_usec = int(round((ts - ts_sec) * 1_000_000))
    if ts_usec >= 1_000_000:
        ts_sec += 1
        ts_usec -= 1_000_000
    return struct.pack("<IIII", ts_sec, ts_usec, pkt_len, pkt_len)


def normalize_ws_base_url(url_str: str) -> str:
    """Normalizes an HTTP or WebSocket URL to a clean base WebSocket URL (scheme://host:port)."""
    url_clean = url_str.strip()
    if url_clean.startswith("http://"):
        url_clean = "ws://" + url_clean[7:]
    elif url_clean.startswith("https://"):
        url_clean = "wss://" + url_clean[8:]
    elif not url_clean.startswith("ws://") and not url_clean.startswith("wss://"):
        url_clean = "ws://" + url_clean

    parsed = urlparse(url_clean)
    netloc = parsed.netloc or parsed.path.split("/")[0]
    scheme = parsed.scheme if parsed.scheme in ("ws", "wss") else "ws"
    return f"{scheme}://{netloc}"


def is_ws_closed(ws: Any) -> bool:
    """Checks whether a WebSocket connection is closed or closing across websockets versions."""
    if ws is None:
        return True
    if getattr(ws, "close_code", None) is not None:
        return True
    if hasattr(ws, "state"):
        try:
            from websockets.protocol import State
            if ws.state != State.OPEN:
                return True
        except Exception:
            if getattr(ws, "state", None) != 1:
                return True
    if getattr(ws, "closed", False):
        return True
    if hasattr(ws, "open") and not ws.open:
        return True
    return False


class BinaryPcapStreamer:
    """
    Background worker that manages concurrent binary WebSocket PCAP streams
    for Wi-Fi and BLE from the edge sensor node to the Central Ingestion Hub.
    """

    def __init__(
        self,
        hub_url: str,
        node_id: str,
        batch_interval_s: float = 0.05,
        batch_max_bytes: int = 32768,
        max_buffer_bytes: int = 50 * 1024 * 1024,  # 50 MB RAM buffer
        quiet: bool = False,
        enable_wifi: bool = True,
        enable_ble: bool = True,
    ):
        self.base_ws_url = normalize_ws_base_url(hub_url)
        self.node_id = str(node_id).strip() or "node"
        self.batch_interval_s = max(0.01, batch_interval_s)
        self.batch_max_bytes = max(1024, batch_max_bytes)
        self.max_buffer_bytes = max(256, max_buffer_bytes)
        self.quiet = quiet
        self.enable_wifi = enable_wifi
        self.enable_ble = enable_ble

        self.wifi_url = f"{self.base_ws_url}/stream/pcap/{self.node_id}/wifi"
        self.ble_url = f"{self.base_ws_url}/stream/pcap/{self.node_id}/ble"

        # Thread-safe in-memory queues
        self.wifi_queue: queue.Queue = queue.Queue()
        self.ble_queue: queue.Queue = queue.Queue()

        self.wifi_buffer_bytes = 0
        self.ble_buffer_bytes = 0
        self.queue_lock = threading.Lock()

        self.running = False
        self.worker_thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        self.wifi_ws: Optional[Any] = None
        self.ble_ws: Optional[Any] = None
        self.wifi_connected = False
        self.ble_connected = False

        self.last_drop_warning = 0.0

        # Statistics
        self.stats = {
            "wifi_packets_enqueued": 0,
            "ble_packets_enqueued": 0,
            "wifi_bytes_sent": 0,
            "ble_bytes_sent": 0,
            "wifi_packets_dropped": 0,
            "ble_packets_dropped": 0,
            "wifi_connect_attempts": 0,
            "ble_connect_attempts": 0,
        }

    def start(self):
        """Starts the background streaming worker thread."""
        if self.running:
            return

        if not self.enable_wifi and not self.enable_ble:
            if not self.quiet:
                logger.info("[*] Binary PCAP streaming skipped (both Wi-Fi and BLE disabled).")
            return

        if websockets is None:
            logger.error("[-] 'websockets' library is required for BinaryPcapStreamer but not installed.")
            return

        self.running = True
        self.worker_thread = threading.Thread(
            target=self._run_event_loop,
            name="BinaryPcapStreamerWorker",
            daemon=True,
        )
        self.worker_thread.start()
        if not self.quiet:
            channels = []
            if self.enable_wifi:
                channels.append("Wi-Fi")
            if self.enable_ble:
                channels.append("BLE")
            logger.info(f"[*] Binary PCAP Streamer active ({' + '.join(channels)}) -> Hub: {self.base_ws_url} (Node ID: {self.node_id})")

    def _run_event_loop(self):
        """Runs the dedicated asyncio event loop for WebSocket connections."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main_async_loop())
        except Exception as e:
            if self.running:
                logger.error(f"[-] Unhandled exception in BinaryPcapStreamer event loop: {e}")
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    async def _main_async_loop(self):
        """Spawns concurrent worker tasks for enabled media streaming."""
        tasks = []
        if self.enable_wifi:
            tasks.append(asyncio.create_task(self._channel_stream_worker("wifi", self.wifi_url, self.wifi_queue)))
        if self.enable_ble:
            tasks.append(asyncio.create_task(self._channel_stream_worker("ble", self.ble_url, self.ble_queue)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            while self.running:
                await asyncio.sleep(0.5)

    async def _channel_stream_worker(self, media: str, ws_url: str, pkt_queue: queue.Queue):
        """Autonomous persistent streaming worker for a single media channel (wifi or ble)."""
        backoff = 1.0
        max_backoff = 15.0

        while self.running:
            if media == "wifi":
                self.stats["wifi_connect_attempts"] += 1
            else:
                self.stats["ble_connect_attempts"] += 1

            try:
                if not self.quiet:
                    logger.debug(f"[*] Connecting to {media.upper()} binary PCAP endpoint: {ws_url}")

                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    if media == "wifi":
                        self.wifi_ws = ws
                        self.wifi_connected = True
                    else:
                        self.ble_ws = ws
                        self.ble_connected = True
                    backoff = 1.0

                    logger.info(f"[+] Connected to Central Hub binary PCAP stream for {media.upper()}: {ws_url}")

                    while self.running:
                        # 1. Proactively detect if connection was closed remotely while idle
                        if is_ws_closed(ws):
                            code = getattr(ws, "close_code", None)
                            raise ConnectionResetError(f"{media.upper()} WebSocket closed remotely (code={code})")

                        # 2. Drain batch from queue
                        batch = self._drain_batch(media, pkt_queue)
                        if batch:
                            await ws.send(batch)
                            if media == "wifi":
                                self.stats["wifi_bytes_sent"] += len(batch)
                            else:
                                self.stats["ble_bytes_sent"] += len(batch)
                        else:
                            # 3. Wait for packets or detect remote socket closure immediately
                            if hasattr(ws, "wait_closed"):
                                try:
                                    await asyncio.wait_for(ws.wait_closed(), timeout=self.batch_interval_s)
                                    code = getattr(ws, "close_code", None)
                                    raise ConnectionResetError(f"{media.upper()} WebSocket closed remotely (code={code})")
                                except (asyncio.TimeoutError, TimeoutError):
                                    pass
                            else:
                                await asyncio.sleep(self.batch_interval_s)

            except Exception as e:
                was_connected = self.wifi_connected if media == "wifi" else self.ble_connected
                if media == "wifi":
                    self.wifi_connected = False
                    self.wifi_ws = None
                else:
                    self.ble_connected = False
                    self.ble_ws = None

                # When a stream loses connection to the Hub, signal the sibling stream
                # so it does not linger on a dead connection while idle
                sibling_media = "ble" if media == "wifi" else "wifi"
                sibling_ws = self.ble_ws if media == "wifi" else self.wifi_ws
                if sibling_ws and not is_ws_closed(sibling_ws):
                    try:
                        logger.debug(f"[*] Hub disconnected on {media.upper()}: signaling {sibling_media.upper()} stream to reconnect.")
                        await sibling_ws.close()
                    except Exception:
                        pass
                if media == "wifi":
                    self.ble_connected = False
                else:
                    self.wifi_connected = False

                if self.running:
                    attempts = (
                        self.stats["wifi_connect_attempts"] if media == "wifi" else self.stats["ble_connect_attempts"]
                    )
                    if was_connected or attempts == 1 or (attempts % 20 == 0):
                        logger.warning(
                            f"[!] {media.upper()} binary PCAP stream connection error ({e}). Retrying in {backoff:.1f}s... (URL: {ws_url})"
                        )
                    elif not self.quiet:
                        logger.debug(f"[-] {media.upper()} binary PCAP stream retry ({e}) in {backoff:.1f}s...")
                    await asyncio.sleep(backoff)
                    backoff = min(max_backoff, backoff * 1.5)

    def _drain_batch(self, media: str, pkt_queue: queue.Queue) -> Optional[bytes]:
        """Drains records from the queue up to batch_max_bytes or until empty."""
        chunks: List[bytes] = []
        bytes_collected = 0

        with self.queue_lock:
            while not pkt_queue.empty() and bytes_collected < self.batch_max_bytes:
                try:
                    record = pkt_queue.get_nowait()
                    chunks.append(record)
                    bytes_collected += len(record)
                    if media == "wifi":
                        self.wifi_buffer_bytes = max(0, self.wifi_buffer_bytes - len(record))
                    else:
                        self.ble_buffer_bytes = max(0, self.ble_buffer_bytes - len(record))
                except queue.Empty:
                    break

        return b"".join(chunks) if chunks else None

    def enqueue_wifi(self, frame: bytes, ts: Optional[float] = None):
        """
        Enqueues a raw 802.11 frame (with Radiotap header).
        Prepends the 16-byte libpcap record header.
        """
        if not self.running or not self.enable_wifi or not frame:
            return

        ts_val = ts if ts is not None else time.time()
        rec_hdr = build_pcap_record_header(ts_val, len(frame))
        record = rec_hdr + frame

        with self.queue_lock:
            # Enforce RAM buffer limits (drop oldest if saturated)
            while (self.wifi_buffer_bytes + len(record)) > self.max_buffer_bytes and not self.wifi_queue.empty():
                try:
                    dropped = self.wifi_queue.get_nowait()
                    self.wifi_buffer_bytes = max(0, self.wifi_buffer_bytes - len(dropped))
                    self.stats["wifi_packets_dropped"] += 1
                except queue.Empty:
                    break

            self.wifi_queue.put(record)
            self.wifi_buffer_bytes += len(record)
            self.stats["wifi_packets_enqueued"] += 1

    def enqueue_ble_raw(self, raw_chunk: bytes):
        """
        Enqueues raw BLE PCAP bytes received from the nRF sniffer pipe.
        Can contain one or more complete PCAP records.
        """
        if not self.running or not self.enable_ble or not raw_chunk:
            return

        with self.queue_lock:
            # Enforce RAM buffer limits
            while (self.ble_buffer_bytes + len(raw_chunk)) > self.max_buffer_bytes and not self.ble_queue.empty():
                try:
                    dropped = self.ble_queue.get_nowait()
                    self.ble_buffer_bytes = max(0, self.ble_buffer_bytes - len(dropped))
                    self.stats["ble_packets_dropped"] += 1
                except queue.Empty:
                    break

            self.ble_queue.put(raw_chunk)
            self.ble_buffer_bytes += len(raw_chunk)
            self.stats["ble_packets_enqueued"] += 1

    def enqueue_ble_frame(self, frame: bytes, ts: Optional[float] = None):
        """
        Enqueues a raw Nordic BLE frame (DLT 272).
        Prepends the 16-byte libpcap record header.
        """
        if not self.running or not self.enable_ble or not frame:
            return

        ts_val = ts if ts is not None else time.time()
        rec_hdr = build_pcap_record_header(ts_val, len(frame))
        record = rec_hdr + frame
        self.enqueue_ble_raw(record)

    def get_stats(self) -> Dict[str, Any]:
        """Returns statistics for both Wi-Fi and BLE streaming channels."""
        with self.queue_lock:
            return {
                **self.stats,
                "enable_wifi": self.enable_wifi,
                "enable_ble": self.enable_ble,
                "wifi_connected": self.wifi_connected,
                "ble_connected": self.ble_connected,
                "wifi_buffer_bytes": self.wifi_buffer_bytes,
                "ble_buffer_bytes": self.ble_buffer_bytes,
                "wifi_queue_size": self.wifi_queue.qsize(),
                "ble_queue_size": self.ble_queue.qsize(),
            }

    def stop(self, timeout: float = 3.0):
        """Stops the streaming worker and cleanly shuts down background tasks."""
        if not self.running:
            return

        self.running = False
        self.wifi_connected = False
        self.ble_connected = False
        if self.loop and self.loop.is_running():
            for ws_obj in (self.wifi_ws, self.ble_ws):
                if ws_obj and not is_ws_closed(ws_obj):
                    try:
                        asyncio.run_coroutine_threadsafe(ws_obj.close(), self.loop)
                    except Exception:
                        pass
        self.wifi_ws = None
        self.ble_ws = None

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=timeout)

        if not self.quiet:
            logger.info("[-] Binary PCAP Streamer stopped.")
