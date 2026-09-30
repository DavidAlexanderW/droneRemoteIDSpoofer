#!/usr/bin/env python3
"""
pcap_logger.py - High-Performance Daily PCAP Capture Logger for Drone Remote ID

Manages daily rotated PCAP capture files per sensor node:
  • 1 daily file for Bluetooth : <pcap_dir>/<node_id>_ble_YYYYMMDD.pcap
  • 1 daily file for Wi-Fi     : <pcap_dir>/<node_id>_wifi_YYYYMMDD.pcap

Uses standard libpcap 2.4 format compatible with Wireshark, tshark, and tcpdump.
Data Link Types (DLT):
  • DLT_IEEE802_11_RADIO (127) for 802.11 frames with Radiotap header.
  • DLT_NORDIC_BLE (272) for Bluetooth LE Link Layer packets with Nordic header.
"""

import os
import sys
import time
import struct
import base64
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, IO

logger = logging.getLogger("DailyPcapLogger")

# Standard libpcap Data Link Types (DLT / Linktype)
DLT_IEEE802_11_RADIO = 127
DLT_NORDIC_BLE = 272

# PCAP file constants (libpcap 2.4 microsecond resolution)
PCAP_MAGIC_MICROSECONDS = 0xa1b2c3d4
PCAP_VERSION_MAJOR = 2
PCAP_VERSION_MINOR = 4
PCAP_GLOBAL_HEADER_LEN = 24
PCAP_RECORD_HEADER_LEN = 16

# ASTM / OpenDroneID Constants
ASTM_OUI = b"\xFA\x0B\xBC"
APP_CODE_RID = 0x0D
BLE_ADV_ACCESS_ADDR = b"\xD6\xBE\x89\x8E"
BLE_RID_UUID = b"\xFA\xFF"  # 0xFFFA (little-endian)


def build_pcap_global_header(dlt: int, snaplen: int = 65535) -> bytes:
    """Builds a 24-byte PCAP Global Header in little-endian format."""
    return struct.pack(
        "<IHHiIII",
        PCAP_MAGIC_MICROSECONDS,
        PCAP_VERSION_MAJOR,
        PCAP_VERSION_MINOR,
        0,        # GMT to local correction (thiszone)
        0,        # Accuracy of timestamps (sigfigs)
        snaplen,  # Max length of captured packets
        dlt,      # Data link type (network)
    )


def build_pcap_record_header(ts: float, pkt_len: int) -> bytes:
    """Builds a 16-byte PCAP Packet Record Header in little-endian format."""
    ts_sec = int(ts)
    ts_usec = int(round((ts - ts_sec) * 1_000_000))
    if ts_usec >= 1_000_000:
        ts_sec += 1
        ts_usec -= 1_000_000
    return struct.pack("<IIII", ts_sec, ts_usec, pkt_len, pkt_len)


def mac_to_bytes(mac: Optional[str]) -> bytes:
    """Parses a MAC address string (e.g. '00:11:22:33:44:55') into 6 bytes."""
    if not mac or mac == "UNKNOWN":
        return b"\x00\x11\x22\x33\x44\x55"
    clean = mac.replace(":", "").replace("-", "").strip()
    if len(clean) == 12:
        try:
            return bytes.fromhex(clean)
        except ValueError:
            pass
    return b"\x00\x11\x22\x33\x44\x55"


def reconstruct_astm_payload(event: Dict[str, Any]) -> bytes:
    """Extracts or reconstructs the 25-byte ASTM Remote ID message block(s)."""
    b64_list = event.get("messages_b64", [])
    payload_parts = []
    for b64_str in b64_list:
        try:
            raw = base64.b64decode(b64_str)
            if raw:
                payload_parts.append(raw[:25])
        except Exception:
            pass
    if payload_parts:
        return b"".join(payload_parts)

    # Fallback to minimal 25-byte Basic ID block
    serial = (event.get("serial_number") or event.get("serial") or "DRONE_RID_001").encode("ascii")
    # Byte 0: 0x00 (Basic ID), Byte 1: 0x10 (ID Type 1, UA Type 0)
    return b"\x00\x10" + serial[:20].ljust(23, b"\x00")


def synthesize_wifi_radiotap_frame(event: Dict[str, Any]) -> bytes:
    """
    Synthesizes a minimal Radiotap + 802.11 Beacon frame containing ASTM Remote ID.
    Used as fallback when only parsed telemetry is available without raw bytes.
    """
    ts = event.get("timestamp", time.time())
    mac_b = mac_to_bytes(event.get("mac"))
    counter = int(event.get("counter", 0)) & 0xFF
    astm_payload = reconstruct_astm_payload(event)

    # 1. Radiotap Header (8 bytes): Rev 0, Pad 0, Length 8, Present flags 0
    radiotap_hdr = b"\x00\x00\x08\x00\x00\x00\x00\x00"

    # 2. 802.11 Beacon Header (24 bytes):
    # Frame Control: 0x8000 (Management / Beacon)
    # Duration: 0x0000
    # DA: FF:FF:FF:FF:FF:FF (Broadcast)
    # SA: mac_b
    # BSSID: mac_b
    # Seq Ctrl: 0x0000
    dot11_hdr = (
        b"\x80\x00"
        b"\x00\x00"
        b"\xFF\xFF\xFF\xFF\xFF\xFF"
        + mac_b
        + mac_b
        + b"\x00\x00"
    )

    # 3. Beacon Fixed Parameters (12 bytes):
    # Timestamp (8B microsecond tick), Beacon Interval 100 TU (2B), Capabilities (2B)
    ts_usec = int(ts * 1_000_000) & 0xFFFFFFFFFFFFFFFF
    fixed_params = struct.pack("<Q", ts_usec) + b"\x64\x00\x21\x04"

    # 4. Tagged Parameters (IEs):
    # SSID IE: Tag 0, Length 0
    ssid_ie = b"\x00\x00"

    # Vendor Specific IE (Tag 221 = 0xDD):
    # ASTM OUI (3B) + App Code 0x0D (1B) + Counter (1B) + Payload
    vendor_data = ASTM_OUI + bytes([APP_CODE_RID, counter]) + astm_payload
    vendor_ie = b"\xDD" + bytes([len(vendor_data)]) + vendor_data

    return radiotap_hdr + dot11_hdr + fixed_params + ssid_ie + vendor_ie


def synthesize_nordic_ble_frame(event: Dict[str, Any]) -> bytes:
    """
    Synthesizes a minimal Nordic BLE frame (DLT 272) containing ASTM Remote ID.
    Used as fallback when only parsed telemetry is available without raw bytes.
    """
    mac_b = mac_to_bytes(event.get("mac"))
    mac_b_le = mac_b[::-1]  # BLE transmits AdvA in little-endian
    counter = int(event.get("counter", 0)) & 0xFF
    astm_payload = reconstruct_astm_payload(event)

    # BLE Advertising PDU Payload (AdvData):
    # 1. Flags AD: len 2, type 0x01, flags 0x06
    flags_ad = b"\x02\x01\x06"

    # 2. Service Data 16-bit UUID AD:
    # AD type 0x16, UUID 0xFFFA (BLE_RID_UUID = \xFA\xFF), App Code 0x0D, Counter, ASTM Payload
    svc_data = BLE_RID_UUID + bytes([APP_CODE_RID, counter]) + astm_payload
    svc_ad = bytes([1 + len(svc_data), 0x16]) + svc_data
    adv_data = flags_ad + svc_ad

    # PDU Header: ADV_NONCONN_IND (0x02), TxAdd random/public, length = len(AdvA) + len(AdvData)
    pdu_payload = mac_b_le + adv_data
    pdu_len = len(pdu_payload)
    pdu_hdr = bytes([0x02, pdu_len])
    ble_pdu = pdu_hdr + pdu_payload

    # Access Address (4 bytes)
    access_addr = BLE_ADV_ACCESS_ADDR

    # Nordic BLE Header (17 bytes):
    # Board(1) + Len(2) + Ver(1) + Cnt(2) + Type(1=0x06) + HdrLen(1=10) + Flags(1=0x01) + Ch(1) + RSSI(1) + EvtCnt(2) + DeltaTime(4)
    rssi_dbm = event.get("rssi_dbm", -60)
    rssi_mag = int(abs(rssi_dbm)) if rssi_dbm is not None else 60
    ch_raw = event.get("channel")
    try:
        ch_num = int(str(ch_raw).replace("Ch", "").strip()) if ch_raw is not None else 37
    except ValueError:
        ch_num = 37

    pkt_total_len = len(access_addr) + len(ble_pdu)
    nordic_hdr = (
        b"\x00"                         # Board 0
        + struct.pack("<H", pkt_total_len)  # Packet length
        + b"\x02"                       # Protocol version 2
        + b"\x00\x00"                   # Packet counter
        + b"\x06"                       # Packet Event
        + b"\x0A"                       # Header length = 10
        + b"\x01"                       # Flags (CRC OK)
        + bytes([ch_num & 0xFF])        # RF Channel
        + bytes([rssi_mag & 0xFF])      # RSSI magnitude
        + b"\x01\x00"                   # Event Counter
        + b"\x00\x00\x00\x00"           # Delta Time
    )

    return nordic_hdr + access_addr + ble_pdu


class DailyNodePcapLogger:
    """
    Manages daily rotated PCAP capture files per node:
      • 1 daily file for Bluetooth: <base_dir>/<prefix><node_id>_ble_YYYYMMDD.pcap
      • 1 daily file for Wi-Fi:     <base_dir>/<prefix><node_id>_wifi_YYYYMMDD.pcap

    Standard libpcap 2.4 format compatible with Wireshark, tshark, tcpdump.
    Handles global PCAP header generation, appending to existing files,
    date rollover based on packet UTC timestamps, and frame synthesis fallbacks.
    """

    def __init__(
        self,
        base_dir: str = "pcaps",
        node_id: Optional[str] = None,
        rotate_daily: bool = True,
        file_prefix: str = "",
        quiet: bool = False,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.node_id = str(node_id or "node").strip()
        self.rotate_daily = rotate_daily
        self.file_prefix = file_prefix.strip()
        self.quiet = quiet

        self.lock = threading.Lock()
        os.makedirs(self.base_dir, exist_ok=True)

        # File handle state for Wi-Fi and Bluetooth keyed by (node_id, media)
        self.handles: Dict[Tuple[str, str], Optional[IO[bytes]]] = {}
        self.current_paths: Dict[Tuple[str, str], Optional[str]] = {}

        # Statistics
        self.stats = {
            "wifi_packets": 0,
            "ble_packets": 0,
            "wifi_bytes": 0,
            "ble_bytes": 0,
        }

    def _resolve_target_path(self, media: str, ts: float, node_id: Optional[str] = None) -> str:
        """Determines the target PCAP file path based on media type, timestamp, and node ID."""
        target_node = str(node_id or self.node_id or "node").strip()
        prefix_part = f"{self.file_prefix}_" if self.file_prefix else ""
        if self.rotate_daily:
            dt_str = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
            filename = f"{prefix_part}{target_node}_{media}_{dt_str}.pcap"
        else:
            filename = f"{prefix_part}{target_node}_{media}.pcap"
        return os.path.join(self.base_dir, filename)

    def _get_handle(self, media: str, ts: float, node_id: Optional[str] = None) -> Tuple[IO[bytes], str]:
        """
        Retrieves or creates the open binary file handle for the requested node, media and date.
        Must be called while self.lock is held.
        """
        target_node = str(node_id or self.node_id or "node").strip()
        target_path = self._resolve_target_path(media, ts, target_node)
        key = (target_node, media)
        handle = self.handles.get(key)
        cur_path = self.current_paths.get(key)

        if handle is not None and cur_path == target_path:
            return handle, target_path

        # Close existing handle if target date/path changed
        if handle is not None:
            try:
                handle.flush()
                handle.close()
            except Exception:
                pass
            self.handles[key] = None

        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        # Determine appropriate Data Link Type (DLT)
        dlt = DLT_IEEE802_11_RADIO if media == "wifi" else DLT_NORDIC_BLE

        # Check if file already exists with valid PCAP header
        file_exists = os.path.exists(target_path)
        file_size = os.path.getsize(target_path) if file_exists else 0

        if not file_exists or file_size < PCAP_GLOBAL_HEADER_LEN:
            # Create new file and write 24-byte PCAP Global Header
            f = open(target_path, "wb")
            hdr = build_pcap_global_header(dlt)
            f.write(hdr)
            f.flush()
            if not self.quiet:
                logger.info(f"[*] Initialized new daily PCAP (Node: {target_node}, {media.upper()} DLT={dlt}): {target_path}")
        else:
            # Append to existing PCAP file
            f = open(target_path, "ab")

        self.handles[key] = f
        self.current_paths[key] = target_path
        return f, target_path

    def write_packet(
        self,
        media: str,
        packet_bytes: bytes,
        ts: Optional[float] = None,
        node_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Writes a single raw packet to the appropriate PCAP file.
        media: 'wifi' or 'ble'
        """
        if not packet_bytes:
            return None

        if ts is None:
            ts = time.time()

        media = "wifi" if media in ("wifi", "nan", "wlan") else "ble"
        target_node = str(node_id or self.node_id or "node").strip()

        with self.lock:
            try:
                handle, target_path = self._get_handle(media, ts, target_node)
                rec_hdr = build_pcap_record_header(ts, len(packet_bytes))
                handle.write(rec_hdr)
                handle.write(packet_bytes)
                handle.flush()

                if media == "wifi":
                    self.stats["wifi_packets"] += 1
                    self.stats["wifi_bytes"] += len(packet_bytes)
                else:
                    self.stats["ble_packets"] += 1
                    self.stats["ble_bytes"] += len(packet_bytes)

                return target_path
            except Exception as e:
                logger.error(f"[-] Error writing {media} PCAP packet for {target_node} to {target_path if 'target_path' in locals() else 'unknown'}: {e}")
                return None

    def write_raw_records(
        self,
        media: str,
        raw_records: bytes,
        node_id: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> Optional[str]:
        """
        Appends pre-formatted binary PCAP record(s) (16-byte record header + packet bytes)
        directly to the node's daily PCAP file.
        media: 'wifi' or 'ble'
        """
        if not raw_records or len(raw_records) < PCAP_RECORD_HEADER_LEN:
            return None

        media = "wifi" if media in ("wifi", "nan", "wlan") else "ble"
        target_node = str(node_id or self.node_id or "node").strip()

        if ts is None:
            try:
                ts_sec = struct.unpack_from("<I", raw_records, 0)[0]
                if 1700000000 <= ts_sec <= 2000000000:
                    ts = float(ts_sec)
                else:
                    ts = time.time()
            except Exception:
                ts = time.time()

        with self.lock:
            try:
                handle, target_path = self._get_handle(media, ts, target_node)
                handle.write(raw_records)
                handle.flush()

                rec_len = len(raw_records)
                if media == "wifi":
                    self.stats["wifi_bytes"] += rec_len
                else:
                    self.stats["ble_bytes"] += rec_len

                # Scan headers to update packet counters
                offset = 0
                count = 0
                while offset + PCAP_RECORD_HEADER_LEN <= rec_len:
                    incl_len = struct.unpack_from("<I", raw_records, offset + 8)[0]
                    offset += PCAP_RECORD_HEADER_LEN + incl_len
                    count += 1

                if media == "wifi":
                    self.stats["wifi_packets"] += count
                else:
                    self.stats["ble_packets"] += count

                return target_path
            except Exception as e:
                logger.error(f"[-] Error writing raw {media} PCAP records for {target_node}: {e}")
                return None

    def log_event(self, event: Dict[str, Any], node_id: Optional[str] = None) -> Optional[str]:
        """
        Extracts raw packet bytes from a parsed event and writes to daily PCAP.
        Supports:
          1. event['raw_bytes']: Direct bytes object.
          2. event['raw_hex']: Hex-encoded string.
          3. Fallback synthesis from event metadata & ASTM messages.
        """
        target_node = str(node_id or event.get("node_id") or self.node_id or "node").strip()
        t_key = str(event.get("transport", "")).lower()
        if t_key in ("bt4", "bt5", "ble", "bluetooth") or "ble" in t_key or "bt" in t_key:
            media = "ble"
        elif t_key in ("wifi", "nan", "wlan") or "wifi" in t_key:
            media = "wifi"
        elif "nrf" in str(event.get("interface", "")).lower() or event.get("pdu_type"):
            media = "ble"
        else:
            media = "wifi"

        # 1. Check for raw_bytes
        raw_b = event.get("raw_bytes")
        if isinstance(raw_b, (bytes, bytearray)) and len(raw_b) > 0:
            pkt_bytes = bytes(raw_b)
        # 2. Check for raw_hex
        elif event.get("raw_hex"):
            try:
                pkt_bytes = bytes.fromhex(event["raw_hex"])
            except Exception:
                pkt_bytes = None
        else:
            pkt_bytes = None

        # 3. Fallback synthesis if raw capture bytes are absent
        if not pkt_bytes:
            if media == "wifi":
                pkt_bytes = synthesize_wifi_radiotap_frame(event)
            else:
                pkt_bytes = synthesize_nordic_ble_frame(event)

        ts = event.get("timestamp") or time.time()
        return self.write_packet(media, pkt_bytes, ts, node_id=target_node)

    def get_stats(self) -> Dict[str, Any]:
        """Returns PCAP capture statistics and current active file paths."""
        with self.lock:
            active_wifi = [p for (n, m), p in self.current_paths.items() if m == "wifi" and p]
            active_ble = [p for (n, m), p in self.current_paths.items() if m == "ble" and p]
            wifi_file = active_wifi[0] if len(active_wifi) == 1 else (active_wifi if active_wifi else None)
            ble_file = active_ble[0] if len(active_ble) == 1 else (active_ble if active_ble else None)
            return {
                **self.stats,
                "current_wifi_file": wifi_file,
                "current_ble_file": ble_file,
                "active_files_by_node": {f"{n}_{m}": p for (n, m), p in self.current_paths.items() if p},
                "base_dir": self.base_dir,
                "node_id": self.node_id,
            }

    def flush(self):
        """Flushes all open PCAP file buffers."""
        with self.lock:
            for handle in self.handles.values():
                if handle:
                    try:
                        handle.flush()
                    except Exception:
                        pass

    def close(self):
        """Flushes and cleanly closes all open PCAP file handles."""
        with self.lock:
            for key, handle in list(self.handles.items()):
                if handle:
                    try:
                        handle.flush()
                        handle.close()
                    except Exception:
                        pass
                self.handles[key] = None
                self.current_paths[key] = None

