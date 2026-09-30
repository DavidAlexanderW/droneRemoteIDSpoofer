#!/usr/bin/env python3
"""
Bluetooth Drone Remote ID Sniffer & Interface Driver
Contains:
1. Autonomous nRF Sniffer UART worker thread with local PCAP streaming (BleNrfSnifferThread).
2. Direct Linux HCI socket raw advertising scanner (RawHciSniffer).
3. Standalone Bluetooth Remote ID scanner CLI (main).
"""

import argparse
import base64
import json
import logging
import os
import queue
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Ensure repository root is in sys.path
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scanner.parser import (
    ASTM_F3411_SpecParser,
    BLE_RID_UUID,
    parse_astm_payload,
)
from scanner.timestamp_utils import is_valid_timestamp

logger = logging.getLogger("CombinedRIDListener.BleSniffer")

REMOTE_ID_UUID = b"\xfa\xff"  # 16-bit UUID in little-endian


# ============================================================================
# Thread 1: Autonomous nRF Sniffer Subprocess Worker Thread
# ============================================================================

class BleNrfSnifferThread(threading.Thread):
    """
    Drives nrf_bt_sniffer_json.py as an autonomous background sniffer subprocess over UART.
    Reads structured JSON records from stdout, normalizes them, and feeds the event queue.
    Provides local socket receiver worker for raw BLE PCAP streaming.
    """

    def __init__(
        self,
        event_queue: queue.Queue,
        nrf_port: Optional[str] = None,
        rx_pcap: Optional[str] = None,
        coded: bool = False,
        ble_mode: str = "hop",
        bt5_dwell_s: float = 5.0,
        bt4_dwell_s: float = 1.0,
        pcap_streamer: Optional[Any] = None,
    ):
        super().__init__(name="BleNrfSnifferThread", daemon=True)
        self.event_queue = event_queue
        self.nrf_port = nrf_port
        self.rx_pcap = rx_pcap
        self.coded = coded
        self.ble_mode = ble_mode
        self.bt5_dwell_s = bt5_dwell_s
        self.bt4_dwell_s = bt4_dwell_s
        self.pcap_streamer = pcap_streamer
        self.running = False
        self.proc: Optional[subprocess.Popen] = None
        self.pcap_server_sock: Optional[socket.socket] = None
        self.pcap_rx_thread: Optional[threading.Thread] = None

    def _pcap_receiver_worker(self, server_sock: socket.socket):
        """Accepts local connection from nrf_bt_sniffer_json.py and feeds raw BLE PCAP chunks into pcap_streamer."""
        while self.running:
            try:
                conn, _ = server_sock.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            conn.settimeout(1.0)
            while self.running and (self.proc and self.proc.poll() is None):
                try:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    if self.pcap_streamer:
                        self.pcap_streamer.enqueue_ble_raw(chunk)
                except socket.timeout:
                    continue
                except Exception:
                    break
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self.running = False
        self.stop_process()

    def run(self):
        self.running = True
        logger.info(
            f"[*] Starting BLE nRF Sniffer worker thread (Mode: {self.ble_mode}, "
            f"BT5: {self.bt5_dwell_s}s, BT4: {self.bt4_dwell_s}s)..."
        )

        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate_paths = [
            os.path.join(script_dir, "nrf_bt_sniffer_json.py"),
            os.path.join(script_dir, "..", "sniffers", "nrf_bt_sniffer_json.py"),
            os.path.join(script_dir, "..", "..", "evaluation", "nrf_bt_sniffer_json.py"),
            os.path.join(script_dir, "..", "..", "nrf_bt_sniffer_json.py"),
        ]
        nrf_script = next((p for p in candidate_paths if os.path.exists(p)), candidate_paths[0])

        # Setup local PCAP tap server if pcap_streamer is attached and BLE PCAP is enabled
        local_pcap_port = None
        if self.pcap_streamer and getattr(self.pcap_streamer, "enable_ble", True):
            try:
                self.pcap_server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.pcap_server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.pcap_server_sock.bind(("127.0.0.1", 0))
                self.pcap_server_sock.listen(1)
                self.pcap_server_sock.settimeout(1.0)
                local_pcap_port = self.pcap_server_sock.getsockname()[1]

                self.pcap_rx_thread = threading.Thread(
                    target=self._pcap_receiver_worker,
                    args=(self.pcap_server_sock,),
                    daemon=True,
                    name="BlePcapTapReceiver",
                )
                self.pcap_rx_thread.start()
            except Exception as e:
                logger.warning(f"[!] Could not initialize local BLE PCAP tap socket: {e}")
                self.pcap_server_sock = None

        while self.running:
            # 1. Detect or verify UART serial port if live
            active_port = self.nrf_port
            if not active_port and not self.rx_pcap:
                for candidate in ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2", "/dev/ttyUSB0", "/dev/ttyUSB1"]:
                    if os.path.exists(candidate):
                        active_port = candidate
                        break

            if not active_port and not self.rx_pcap:
                logger.warning("[!] No nRF BLE sniffer device found on /dev/ttyACM* or /dev/ttyUSB*. Retrying in 3s...")
                time.sleep(3.0)
                continue

            # Kill any lingering nrfutil / sniffer instances
            subprocess.run(
                ["killall", "-9", "nrfutil", "nrfutil-ble-sniffer", "nrfutil-ble-sni"],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            time.sleep(0.3)

            r_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
            venv_python = os.path.join(r_root, ".venv", "bin", "python")
            py_bin = venv_python if os.path.exists(venv_python) else sys.executable

            cmd = [
                py_bin,
                nrf_script,
                "--only-rid",
                "--ble-mode",
                self.ble_mode,
                "--bt5-dwell",
                str(self.bt5_dwell_s),
                "--bt4-dwell",
                str(self.bt4_dwell_s),
            ]
            if local_pcap_port:
                cmd.extend(["--raw-pcap-port", str(local_pcap_port)])
            if self.coded:
                cmd.append("--coded")
            if active_port:
                cmd.extend(["--nrf-port", active_port])
            elif self.rx_pcap and os.path.exists(self.rx_pcap):
                cmd.extend(["--rx-pcap", self.rx_pcap])

            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, start_new_session=True
                )
                port_desc = active_port if active_port else self.rx_pcap
                logger.info(f"[*] BLE nRF Sniffer process active on {port_desc}.")

                while self.running and self.proc.poll() is None:
                    line = self.proc.stdout.readline()
                    if not line:
                        continue

                    line_str = line.strip()
                    if not line_str.startswith("{"):
                        continue

                    try:
                        record = json.loads(line_str)
                        mac = record.get("mac")
                        now_ts = time.time()
                        ts = record.get("timestamp")
                        # Validate timestamp with dynamic bounds
                        if not is_valid_timestamp(ts):
                            ts = now_ts

                        rid_info = record.get("remote_id")

                        if not mac or mac == "UNKNOWN" or not rid_info:
                            continue

                        parsed_msgs = rid_info.get("parsed_messages", [])
                        raw_hex = record.get("raw_hex", "")
                        transport_type = str(rid_info.get("transport", "")).lower()
                        pdu_type = str(record.get("pdu_type", "")).upper()
                        active_mode = str(record.get("active_ble_mode", "")).lower()

                        if (
                            "5" in transport_type
                            or "ext" in transport_type
                            or "AUX" in pdu_type
                            or "EXT" in pdu_type
                        ):
                            transport = "bt5"
                        elif "4" in transport_type or "legacy" in transport_type or active_mode == "bt4":
                            transport = "bt4"
                        elif active_mode == "bt5":
                            transport = "bt5"
                        else:
                            transport = "bt5" if self.ble_mode in ("ble5", "extended") else "bt4"

                        # If parsed_msgs is empty or missing fields, try parsing raw_hex if available
                        if not parsed_msgs and raw_hex:
                            try:
                                raw_bytes = bytes.fromhex(raw_hex)
                                uuid_idx = raw_bytes.find(BLE_RID_UUID)
                                if uuid_idx != -1:
                                    astm_p = raw_bytes[uuid_idx + 2 :]
                                    if len(astm_p) >= 2 and astm_p[0] == 0x0D:
                                        astm_p = astm_p[2:]
                                    parsed_msgs, _ = parse_astm_payload(astm_p)
                            except Exception:
                                pass

                        # Generate messages_b64 from parsed message hex blocks or raw
                        messages_b64 = []
                        for msg in parsed_msgs:
                            if isinstance(msg, dict) and "raw_hex" in msg:
                                raw_b = bytes.fromhex(msg["raw_hex"])
                                messages_b64.append(base64.b64encode(raw_b[:25]).decode("ascii"))

                        serial_no = None
                        for msg in parsed_msgs:
                            if isinstance(msg, dict) and msg.get("type") == "Basic ID" and msg.get("id"):
                                serial_no = msg["id"]
                                break

                        counter = rid_info.get("counter", 0)

                        rf_ch = record.get("rf_channel")
                        ch_str = f"Ch {rf_ch}" if rf_ch is not None else "Adv (37/38/39)"
                        freq_mhz = 2402
                        if rf_ch == 37:
                            freq_mhz = 2402
                        elif rf_ch == 38:
                            freq_mhz = 2426
                        elif rf_ch == 39:
                            freq_mhz = 2480
                        elif rf_ch is not None and 0 <= rf_ch <= 36:
                            freq_mhz = 2404 + 2 * rf_ch

                        event: Dict[str, Any] = {
                            "timestamp": ts,
                            "reception_timestamp": ts,
                            "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                            "transport": transport,
                            "interface": "nRF52840-UART",
                            "channel": ch_str,
                            "band": "2.4GHz",
                            "frequency_mhz": freq_mhz,
                            "rate_mbps": 1.0,
                            "modulation": "GFSK",
                            "rate_desc": "1.0 Mbps (LE 1M GFSK)",
                            "bandwidth_mhz": 2,
                            "mcs_index": None,
                            "guard_interval": None,
                            "counter": counter,
                            "mac": mac.upper(),
                            "rssi_dbm": record.get("rssi_dbm"),
                            "serial_number": serial_no,
                            "messages": parsed_msgs,
                            "messages_b64": messages_b64,
                            "raw_length": record.get("raw_length", 0),
                            "raw_hex": raw_hex,
                            "raw_bytes": bytes.fromhex(raw_hex) if raw_hex else None,
                            "pdu_type": record.get("pdu_type"),
                        }

                        self.event_queue.put(event)

                    except Exception as e:
                        logger.debug(f"Error parsing BLE JSON record: {e}")

            except Exception as e:
                if self.running:
                    logger.error(f"[-] Error running nrf_bt_sniffer_json.py: {e}")
            finally:
                self.stop_process()

            if self.rx_pcap or not self.running:
                break

            logger.warning("[!] BLE nRF sniffer disconnected or exited. Reconnecting in 2.0s...")
            time.sleep(2.0)

        self.running = False
        logger.info("[*] BLE nRF Sniffer worker stopped.")

    def stop_process(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                time.sleep(0.1)
                if self.proc.poll() is None:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

        if self.pcap_server_sock:
            try:
                self.pcap_server_sock.close()
            except Exception:
                pass
            self.pcap_server_sock = None


# ============================================================================
# Standalone HCI Linux Raw Socket Sniffer
# ============================================================================

START_TIME = None
REPLAY_FILE = None
MAC_TO_SERIAL: Dict[str, str] = {}


def parse_ad_structures(data: bytes):
    """Parse LTV structures from raw AD payload and return service data dict for 16-bit UUIDs."""
    service_data = {}
    i = 0
    while i < len(data):
        length = data[i]
        if length == 0:
            break
        if i + 1 + length > len(data):
            break
        ad_type = data[i + 1]
        ad_value = data[i + 2 : i + 1 + length]

        if ad_type == 0x16 and len(ad_value) >= 2:  # Service Data - 16-bit UUID
            uuid16 = ad_value[:2]
            service_data[uuid16] = ad_value[2:]

        i += 1 + length
    return service_data


def handle_payload(mac: str, rssi: int, service_data_payload: bytes):
    print(f"🚁 Drone Detected! MAC: {mac} | RSSI: {rssi} dBm")
    print(f"  Raw Remote ID Payload (Hex): {service_data_payload.hex().upper()}")
    try:
        counter = service_data_payload[1] if len(service_data_payload) >= 2 and service_data_payload[0] == 0x0D else 0
        astm_data = (
            service_data_payload[2:]
            if len(service_data_payload) >= 2 and service_data_payload[0] == 0x0D
            else service_data_payload
        )

        msg_type = (astm_data[0] >> 4) if len(astm_data) > 0 else None
        is_bt5 = len(astm_data) > 25 or msg_type == 0xF
        transport = "bt5" if is_bt5 else "bt4"

        if is_bt5:
            print("  [Protocol] BLE 5 Extended Advertising (Message Pack / Multi-Message)")
        else:
            print("  [Protocol] BLE 4 Legacy Advertising (Single Message)")

        parsed_data, msgs_b64 = parse_astm_payload(astm_data)

        serial = None
        if parsed_data:
            for entry in parsed_data:
                print(f"    - {entry}")
                if entry.get("type") == "Basic ID" and entry.get("id"):
                    serial = entry["id"]
                    MAC_TO_SERIAL[mac] = serial
        else:
            print(f"    - (Parser returned no data. astm_data length: {len(astm_data)}, type: {msg_type})")

        if REPLAY_FILE is not None and msgs_b64:
            now_ts = time.time()
            event = {
                "timestamp": now_ts,
                "timestamp_iso": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
                "time_offset_ms": int((now_ts - START_TIME) * 1000) if START_TIME else 0,
                "transport": transport,
                "counter": counter,
                "messages_b64": msgs_b64,
                "mac": mac,
            }
            if serial:
                event["serial"] = serial
            elif mac in MAC_TO_SERIAL:
                event["serial"] = MAC_TO_SERIAL[mac]

            REPLAY_FILE.write(json.dumps(event) + "\n")
            REPLAY_FILE.flush()

    except Exception as e:
        import traceback
        print(f"    - Error parsing payload: {type(e).__name__}: {e}")
        traceback.print_exc()
    print("-" * 50)


class RawHciSniffer:

    def __init__(self, adapter="hci0"):
        self.adapter = adapter
        self.dev_id = int(adapter.replace("hci", "")) if adapter.startswith("hci") else 0
        self.sock = None

    def _build_hci_command(self, opcode: int, data: bytes) -> bytes:
        return struct.pack("<BHB", 0x01, opcode, len(data)) + data

    def start(self):
        try:
            self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
            self.sock.bind((self.dev_id,))
        except Exception as e:
            raise RuntimeError(f"Failed to open HCI socket on {self.adapter}. Did you use sudo? Error: {e}")

        type_mask = 1 << 4
        event_mask_lo = 0xFFFFFFFF
        event_mask_hi = 0xFFFFFFFF
        filter_bytes = struct.pack("<IIIH", type_mask, event_mask_lo, event_mask_hi, 0) + b"\x00\x00"
        self.sock.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, filter_bytes)

        try:
            self.sock.send(self._build_hci_command(0x2042, struct.pack("<BB", 0x00, 0x00)))
            params = struct.pack(
                "<BBB BHH BHH", 0x01, 0x00, 0x05, 0x01, 0x0010, 0x0010, 0x01, 0x0010, 0x0010
            )
            self.sock.send(self._build_hci_command(0x2041, params))
            self.sock.send(self._build_hci_command(0x2042, struct.pack("<BBHH", 0x01, 0x00, 0x0000, 0x0000)))
        except OSError as e:
            if e.errno == 97:
                print(
                    f"[!] Warning: Adapter {self.adapter} rejected Extended Scanning commands (BT 4.0 adapter?). "
                    "Falling back to Legacy Scanning..."
                )
                self.sock.send(
                    self._build_hci_command(0x200B, struct.pack("<BHHBB", 0x01, 0x0010, 0x0010, 0x00, 0x00))
                )
                self.sock.send(self._build_hci_command(0x200C, struct.pack("<BB", 0x01, 0x00)))
            else:
                raise

        print(f"[*] Raw HCI Scanner started on {self.adapter} (100% duty cycle)")

    def stop(self):
        if self.sock:
            try:
                self.sock.send(self._build_hci_command(0x2042, struct.pack("<BB", 0x00, 0x00)))
                self.sock.send(self._build_hci_command(0x200C, struct.pack("<BB", 0x00, 0x00)))
            except Exception:
                pass
            self.sock.close()

    def process_events(self):
        while True:
            ready, _, _ = select.select([self.sock], [], [], 1.0)
            if not ready:
                continue

            try:
                pkt = self.sock.recv(258)
            except OSError:
                break

            if len(pkt) < 3 or pkt[0] != 0x04:
                continue

            event_code = pkt[1]
            if event_code == 0x3E:
                subevent = pkt[3]
                if subevent == 0x0D:
                    num_reports = pkt[4]
                    offset = 5
                    for _ in range(num_reports):
                        if offset + 24 > len(pkt):
                            break
                        (
                            evt_type,
                            addr_type,
                            addr,
                            p_phy,
                            s_phy,
                            sid,
                            tx_pwr,
                            rssi,
                            p_int,
                            d_addr_type,
                            d_addr,
                            d_len,
                        ) = struct.unpack("<HB6sBBBbbHB6sB", pkt[offset : offset + 24])
                        offset += 24
                        data = pkt[offset : offset + d_len]
                        offset += d_len

                        self._process_ad_data(addr, rssi, data)

                elif subevent == 0x02:
                    num_reports = pkt[4]
                    offset = 5
                    for _ in range(num_reports):
                        if offset + 9 > len(pkt):
                            break
                        evt_type, addr_type, addr, d_len = struct.unpack("<BB6sB", pkt[offset : offset + 9])
                        offset += 9
                        data = pkt[offset : offset + d_len]
                        offset += d_len
                        rssi = 0
                        if offset < len(pkt):
                            rssi = struct.unpack("<b", pkt[offset : offset + 1])[0]
                            offset += 1

                        self._process_ad_data(addr, rssi, data)

    def _process_ad_data(self, addr_bytes: bytes, rssi: int, data: bytes):
        ad_structures = parse_ad_structures(data)
        if REMOTE_ID_UUID in ad_structures:
            mac = ":".join(f"{b:02X}" for b in reversed(addr_bytes))
            handle_payload(mac, rssi, ad_structures[REMOTE_ID_UUID])


def main():
    parser = argparse.ArgumentParser(description="Raw HCI BT5 Drone Remote ID Sniffer")
    parser.add_argument("--replay-out", help="Optional output JSONL file for use with replay_drones.py", default=None)
    parser.add_argument("--adapter", help="Bluetooth adapter to use (e.g. hci0)", default="hci0")
    args = parser.parse_args()

    global START_TIME, REPLAY_FILE
    START_TIME = time.time()

    if args.replay_out:
        REPLAY_FILE = open(args.replay_out, "a")
        print(f"[*] Replay events will be saved to {args.replay_out}")

    sniffer = RawHciSniffer(args.adapter)
    try:
        sniffer.start()
        sniffer.process_events()
    except KeyboardInterrupt:
        print("\nScanning stopped.")
    finally:
        sniffer.stop()
        if REPLAY_FILE:
            REPLAY_FILE.close()


if __name__ == "__main__":
    main()
