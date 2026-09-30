#!/usr/bin/env python3
"""
Wi-Fi Drone Remote ID Sniffer & Interface Driver
Contains:
1. Low-level Linux monitor mode configuration (setup_monitor_mode, restore_managed_mode).
2. Multi-band channel hopping engine (WifiChannelHopperThread).
3. High-throughput AF_PACKET raw socket receiver (WifiSnifferThread).
4. Standalone Scapy-based Wi-Fi Remote ID scanner CLI (main).
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
from typing import Any, Dict, List, Optional

# Ensure repository root is in sys.path
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scanner.channel_state import (
    NON_SOCIAL_CHANNELS_2G,
    NON_SOCIAL_CHANNELS_5G,
    SOCIAL_CHANNEL_2G,
    SOCIAL_CHANNEL_5G,
    SharedChannelState,
    get_freq_for_channel,
)
from scanner.parser import (
    APP_CODE_RID,
    ASTM_F3411_SpecParser,
    ASTM_OUI,
    parse_astm_payload,
)
from scanner.radiotap import extract_radiotap_phy_info

logger = logging.getLogger("CombinedRIDListener.WifiSniffer")


# ============================================================================
# Monitor Mode Interface Administration
# ============================================================================

def setup_monitor_mode(interface: str, initial_channel: int = 6):
    """Put interface into monitor mode, unmanage from NetworkManager, and bring it up."""
    logger.info(f"[*] Configuring {interface} into monitor mode...")
    try:
        # Attempt to unmanage from NetworkManager to prevent channel hopping interference
        try:
            subprocess.run(
                ["nmcli", "dev", "set", interface, "managed", "no"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

        subprocess.run(["ip", "link", "set", interface, "down"], check=True)
        subprocess.run(["iw", "dev", interface, "set", "type", "monitor"], check=True)
        subprocess.run(["ip", "link", "set", interface, "up"], check=True)
        subprocess.run(["iw", "dev", interface, "set", "channel", str(initial_channel)], check=True)
        logger.info(f"[*] {interface} is ready in monitor mode on Channel {initial_channel}.")
    except Exception as e:
        logger.warning(f"[!] Warning: Could not configure monitor mode on {interface}: {e}")


def restore_managed_mode(interface: str):
    """Restore interface to managed mode."""
    logger.info(f"[*] Restoring {interface} to managed mode...")
    try:
        subprocess.run(["ip", "link", "set", interface, "down"], check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["iw", "dev", interface, "set", "type", "managed"], check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["ip", "link", "set", interface, "up"], check=True, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.debug(f"Failed to restore {interface}: {e}")


# Backward-compatible alias
teardown_monitor_mode = restore_managed_mode


# ============================================================================
# Thread 1: Wi-Fi Channel Hopper Engine
# ============================================================================

class WifiChannelHopperThread(threading.Thread):
    """
    Dedicated thread executing the Wi-Fi Remote ID channel hopping schedule:
    - 2.4 GHz Social Channel: Ch 6 (1000 ms dwell)
    - 2.4 GHz Non-Social Channels: (200 ms dwell each)
    - 5.8 GHz Social Channel: Ch 149 (1000 ms dwell)
    - 5.8 GHz Non-Social Channels: (extended 250 ms dwell to compensate for mixed/negative PLL lead times)
    - Configurable non-social ratio (2k on 2.4GHz for every k on 5.8GHz)
    - Transition-Aware Packet Attribution: Packets arriving within the drain retention
      window (< 5ms) are attributed to the previous channel; all subsequent packets
      received during and after the switch are attributed to the target channel.
    """

    def __init__(
        self,
        interface: str,
        channel_state: SharedChannelState,
        non_social_ratio_k: int = 1,
        social_dwell_ms: int = 1000,
        non_social_dwell_ms: int = 200,
        non_social_dwell_5g_ms: Optional[int] = 250,
    ):
        super().__init__(name="WifiHopperThread", daemon=True)
        self.interface = interface
        self.channel_state = channel_state
        self.k = max(1, non_social_ratio_k)
        self.social_dwell_s = social_dwell_ms / 1000.0
        self.non_social_dwell_2g_s = non_social_dwell_ms / 1000.0
        self.non_social_dwell_5g_s = (
            non_social_dwell_5g_ms if non_social_dwell_5g_ms is not None else max(non_social_dwell_ms, 250)
        ) / 1000.0
        self.running = False
        self.current_channel = 6

        self.n_2g_non_social = 2 * self.k
        self.n_5g_non_social = self.k

        self.idx_2g = 0
        self.idx_5g = 0

    def _set_channel(self, channel: int) -> bool:
        # Method 1: iw dev <iface> set channel <ch>
        try:
            res = subprocess.run(
                ["iw", "dev", self.interface, "set", "channel", str(channel)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass

        # Method 2: iwconfig <iface> channel <ch>
        try:
            res = subprocess.run(
                ["iwconfig", self.interface, "channel", str(channel)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass

        # Method 3: iw dev <iface> set freq <freq_mhz>
        freq = get_freq_for_channel(channel)
        if freq > 0:
            try:
                res = subprocess.run(
                    ["iw", "dev", self.interface, "set", "freq", str(freq)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if res.returncode == 0:
                    return True
            except Exception:
                pass

        return False

    def _hop_step(self, target_channel: int, target_dwell_s: float):
        if not self.running:
            return

        self.channel_state.start_switch(target_channel, source_channel=self.current_channel)
        success = self._set_channel(target_channel)
        if success:
            self.current_channel = target_channel
        self.channel_state.finish_switch(target_channel)

        if not self.running:
            return

        time.sleep(target_dwell_s)

    def run(self):
        self.running = True
        logger.info(
            f"[*] Wi-Fi Hopper started on {self.interface} "
            f"(2.4G Non-Social: {self.n_2g_non_social}/cycle, 5.8G Non-Social: {self.n_5g_non_social}/cycle, "
            f"Social Dwell: {self.social_dwell_s*1000:.0f}ms, 2.4G Non-Social: {self.non_social_dwell_2g_s*1000:.0f}ms, "
            f"5.8G Non-Social: {self.non_social_dwell_5g_s*1000:.0f}ms)"
        )

        self._set_channel(SOCIAL_CHANNEL_2G)
        self.channel_state.update(SOCIAL_CHANNEL_2G)

        try:
            while self.running:
                # --- Step 1: 2.4 GHz Social Channel (Ch 6) #1 ---
                self._hop_step(SOCIAL_CHANNEL_2G, self.social_dwell_s)

                # --- Step 2: 2.4 GHz Non-Social Channels (2k channels) ---
                for _ in range(self.n_2g_non_social):
                    if not self.running:
                        break
                    ch_2g = NON_SOCIAL_CHANNELS_2G[self.idx_2g % len(NON_SOCIAL_CHANNELS_2G)]
                    self.idx_2g += 1
                    self._hop_step(ch_2g, self.non_social_dwell_2g_s)

                # --- Step 3: 2.4 GHz Social Channel (Ch 6) #2 (Priority Channel 6 Return) ---
                self._hop_step(SOCIAL_CHANNEL_2G, self.social_dwell_s)

                # --- Step 4: 5.8 GHz Social Channel (Ch 149) ---
                self._hop_step(SOCIAL_CHANNEL_5G, self.social_dwell_s)

                # --- Step 5: 5.8 GHz Non-Social Channels (k channels) ---
                for _ in range(self.n_5g_non_social):
                    if not self.running:
                        break
                    ch_5g = NON_SOCIAL_CHANNELS_5G[self.idx_5g % len(NON_SOCIAL_CHANNELS_5G)]
                    self.idx_5g += 1
                    self._hop_step(ch_5g, self.non_social_dwell_5g_s)

        except Exception as e:
            if self.running:
                logger.error(f"[-] Wi-Fi Hopper encountered error: {e}")
        finally:
            self.running = False
            logger.info("[*] Wi-Fi Channel Hopper stopped.")

    def stop(self):
        self.running = False


# ============================================================================
# Thread 2: High-Performance Raw Socket Wi-Fi Sniffer
# ============================================================================

class WifiSnifferThread(threading.Thread):
    """
    Captures raw 802.11 frames on monitor-mode interface using AF_PACKET raw socket.
    Parses Wi-Fi Beacon Vendor Specific Elements (FA:0B:BC) and NAN Action frames.
    """

    def __init__(
        self,
        interface: str,
        channel_state: SharedChannelState,
        event_queue: queue.Queue,
        pcap_streamer: Optional[Any] = None,
    ):
        super().__init__(name="WifiSnifferThread", daemon=True)
        self.interface = interface
        self.channel_state = channel_state
        self.event_queue = event_queue
        self.pcap_streamer = pcap_streamer
        self.running = False
        self.sock: Optional[socket.socket] = None

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def run(self):
        self.running = True
        logger.info(f"[*] Starting Wi-Fi Sniffer on {self.interface}...")

        try:
            self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
            self.sock.bind((self.interface, 0))
        except Exception as e:
            logger.error(f"[-] Failed to bind AF_PACKET raw socket on {self.interface}: {e}. (Need root / sudo)")
            self.running = False
            return

        while self.running:
            try:
                ready = select.select([self.sock], [], [], 0.1)
                if not ready[0]:
                    continue

                frame = self.sock.recv(4096)
                if len(frame) < 24:
                    continue

                ts = time.time()

                # Tapping raw frame for concurrent binary PCAP streaming
                if self.pcap_streamer and getattr(self.pcap_streamer, "enable_wifi", True):
                    self.pcap_streamer.enqueue_wifi(frame, ts)

                # Fast check for ASTM OUI (FA:0B:BC) in Vendor Specific IEs (0xDD) or NAN Action frames
                vendor_ie_idx = -1
                search_offset = 0
                while True:
                    idx = frame.find(ASTM_OUI, search_offset)
                    if idx == -1:
                        break
                    if idx >= 2 and frame[idx - 2] == 0xDD:
                        vendor_ie_idx = idx
                        break
                    search_offset = idx + 1

                is_nan = False
                radiotap_len = struct.unpack('<H', frame[2:4])[0] if (len(frame) >= 4 and frame[0] == 0x00) else 0

                if vendor_ie_idx == -1:
                    # Check for NAN Action frames (0xD0 / 0xE0)
                    if len(frame) > radiotap_len + 24:
                        fc = frame[radiotap_len]
                        if fc in (0xD0, 0xE0):
                            for offset in range(radiotap_len + 24, min(len(frame) - 4, radiotap_len + 128)):
                                if (frame[offset] >> 4) == 0xF and frame[offset + 1] == 0x19:
                                    is_nan = True
                                    nan_pack_offset = offset
                                    break
                    if not is_nan:
                        continue

                if len(frame) < radiotap_len + 24:
                    continue

                # Extract MAC Address (addr2 / transmitter address at offset radiotap_len + 10)
                mac_bytes = frame[radiotap_len + 10 : radiotap_len + 16]
                mac_addr = ':'.join(f'{b:02X}' for b in mac_bytes)

                # Extract PHY RF parameters (RSSI, data rate, modulation, channel freq)
                phy_info = extract_radiotap_phy_info(frame)
                rssi_dbm = phy_info.get("rssi_dbm")
                rate_mbps = phy_info.get("rate_mbps")
                modulation = phy_info.get("modulation")
                rate_desc = phy_info.get("rate_desc")
                bandwidth_mhz = phy_info.get("bandwidth_mhz")
                mcs_index = phy_info.get("mcs_index")
                guard_interval = phy_info.get("guard_interval")
                cur_ch, cur_band, cur_freq = self.channel_state.resolve_channel(ts, phy_info.get("frequency_mhz"))

                # Extract Payload
                counter = 0
                if is_nan:
                    transport = "nan"
                    astm_payload = frame[nan_pack_offset:]
                else:
                    transport = "wifi"
                    ie_len = frame[vendor_ie_idx - 1]
                    if vendor_ie_idx + ie_len > len(frame):
                        continue
                    vendor_data = frame[vendor_ie_idx + 3 : vendor_ie_idx + ie_len]
                    if len(vendor_data) >= 2 and vendor_data[0] == APP_CODE_RID:
                        counter = vendor_data[1]
                        astm_payload = vendor_data[2:]
                    else:
                        counter = 0
                        astm_payload = vendor_data

                # Decode ASTM Messages and raw 25-byte blocks
                parsed_messages, messages_b64 = parse_astm_payload(astm_payload)
                if not parsed_messages:
                    continue

                serial_no = None
                for msg in parsed_messages:
                    if msg.get("type") == "Basic ID" and msg.get("id") and msg.get("is_known_version", True):
                        serial_no = msg["id"]
                        break

                event: Dict[str, Any] = {
                    "timestamp": ts,
                    "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    "transport": transport,
                    "interface": self.interface,
                    "channel": cur_ch,
                    "band": cur_band,
                    "frequency_mhz": cur_freq,
                    "rate_mbps": rate_mbps,
                    "modulation": modulation,
                    "rate_desc": rate_desc,
                    "bandwidth_mhz": bandwidth_mhz,
                    "mcs_index": mcs_index,
                    "guard_interval": guard_interval,
                    "counter": counter,
                    "mac": mac_addr,
                    "rssi_dbm": rssi_dbm,
                    "serial_number": serial_no,
                    "messages": parsed_messages,
                    "messages_b64": messages_b64,
                    "raw_length": len(frame),
                    "raw_hex": frame.hex().upper(),
                    "raw_bytes": frame,
                }

                self.event_queue.put(event)

            except Exception as e:
                if self.running:
                    logger.debug(f"Error processing Wi-Fi frame: {e}")

        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        logger.info("[*] Wi-Fi Sniffer stopped.")


# ============================================================================
# Standalone Scapy Sniffer CLI Implementation
# ============================================================================

START_TIME = None
REPLAY_FILE = None
PCAP_FILE = None
MAC_TO_SERIAL: Dict[str, str] = {}


def process_packet(pkt):
    from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Elt
    from scapy.utils import wrpcap

    if not pkt.haslayer(Dot11):
        return

    mac_addr = pkt.addr2
    if not mac_addr:
        return

    rssi = "Unknown"
    rate_mbps = None
    modulation = None
    rate_desc = None
    if pkt.haslayer("RadioTap"):
        try:
            rt = pkt["RadioTap"]
            rssi_val = getattr(rt, "dBm_AntSignal", None)
            if rssi_val is not None:
                rssi = f"{rssi_val} dBm"
            raw_rate = getattr(rt, "Rate", None)
            if raw_rate is not None:
                rate_mbps = round(float(raw_rate) * 0.5, 1)
                if rate_mbps <= 2.0:
                    modulation = "DSSS"
                elif rate_mbps in (5.5, 11.0):
                    modulation = "CCK"
                elif rate_mbps in (6.0, 9.0, 12.0, 18.0, 24.0, 36.0, 48.0, 54.0):
                    modulation = "OFDM"
                else:
                    modulation = "802.11"
                rate_desc = f"{rate_mbps:.1f} Mbps {modulation}"
        except Exception:
            pass

    if pkt.haslayer(Dot11Beacon):
        current = pkt.getlayer(Dot11Beacon).payload
        found_astm = False
        ssid_val = None
        rates_val = None
        dsset_val = None
        tim_val = None
        erp_val = None
        esr_val = None
        astm_vendor_data = None

        while current:
            if isinstance(current, Dot11Elt):
                if current.ID == 0:
                    try:
                        ssid_val = current.info.decode("ascii", errors="ignore")
                    except Exception:
                        pass
                elif current.ID == 1:
                    rates_val = current.info
                elif current.ID == 3:
                    dsset_val = current.info
                elif current.ID == 5:
                    tim_val = current.info
                elif current.ID == 42:
                    erp_val = current.info
                elif current.ID == 50:
                    esr_val = current.info
                elif current.ID == 221:
                    info = current.info
                    if info.startswith(b"\xfa\x0b\xbc"):
                        vendor_data = info[3:]
                        if len(vendor_data) > 0 and vendor_data[0] == 0x0D:
                            found_astm = True
                            astm_vendor_data = vendor_data
            current = current.payload

        if found_astm:
            _handle_astm_payload(
                pkt,
                mac_addr,
                rssi,
                astm_vendor_data,
                ssid_val,
                rates_val,
                dsset_val,
                tim_val,
                erp_val,
                esr_val,
                transport="wifi",
                rate_mbps=rate_mbps,
                modulation=modulation,
                rate_desc=rate_desc,
            )
            if PCAP_FILE is not None:
                wrpcap(PCAP_FILE, pkt, append=True)
            return

    if pkt.type == 0 and pkt.subtype in (13, 14):
        raw = bytes(pkt)
        for i in range(len(raw) - 4):
            pack_hdr = raw[i + 1]
            if (pack_hdr >> 4) == 0xF and raw[i + 2] == 0x19:
                msg_count = raw[i + 3]
                if 1 <= msg_count <= 10:
                    expected_len = msg_count * 25
                    if i + 4 + expected_len <= len(raw):
                        first_msg_type = raw[i + 4] >> 4
                        if 0 <= first_msg_type <= 5:
                            vendor_data = b"\x0D" + raw[i : i + 4 + expected_len]
                            _handle_astm_payload(
                                pkt,
                                mac_addr,
                                rssi,
                                vendor_data,
                                None,
                                None,
                                None,
                                None,
                                None,
                                None,
                                transport="nan",
                                rate_mbps=rate_mbps,
                                modulation=modulation,
                                rate_desc=rate_desc,
                            )
                            if PCAP_FILE is not None:
                                wrpcap(PCAP_FILE, pkt, append=True)
                            return


def _handle_astm_payload(
    pkt,
    mac_addr,
    rssi,
    vendor_data,
    ssid_val,
    rates_val,
    dsset_val,
    tim_val,
    erp_val,
    esr_val,
    transport="wifi",
    rate_mbps=None,
    modulation=None,
    rate_desc=None,
):
    try:
        counter = vendor_data[1] if len(vendor_data) > 1 else 0
        pack_hdr = vendor_data[2] if len(vendor_data) > 2 else 0
        msg_type = pack_hdr >> 4

        if msg_type == 0xF and len(vendor_data) > 4 and vendor_data[3] == 0x19:
            t_str = "Wi-Fi NAN" if transport == "nan" else "Wi-Fi Beacon"
            phy_str = f" | PHY: {rate_desc}" if rate_desc else ""
            print(f"🚁 {t_str} Drone Detected! MAC: {mac_addr} | RSSI: {rssi}{phy_str}")
            print(f"  Raw Remote ID Payload (Hex): {vendor_data.hex().upper()}")

            parsed_data, msgs_b64 = parse_astm_payload(vendor_data[2:])

            serial = None
            if parsed_data:
                for entry in parsed_data:
                    print(f"    - {entry}")
                    if entry.get("type") == "Basic ID" and entry.get("id"):
                        serial = entry["id"]
                        MAC_TO_SERIAL[mac_addr] = serial
            else:
                print("    - (Parser returned no data)")

            if REPLAY_FILE is not None and msgs_b64:
                global START_TIME
                if START_TIME is None:
                    START_TIME = float(pkt.time)

                event = {
                    "time_offset_ms": int((float(pkt.time) - START_TIME) * 1000),
                    "transport": transport,
                    "counter": counter,
                    "messages_b64": msgs_b64,
                    "mac": mac_addr,
                    "rate_mbps": rate_mbps,
                    "modulation": modulation,
                    "rate_desc": rate_desc,
                }
                if serial:
                    event["serial"] = serial
                elif mac_addr in MAC_TO_SERIAL:
                    event["serial"] = MAC_TO_SERIAL[mac_addr]

                if ssid_val:
                    event["ssid"] = ssid_val
                if rates_val:
                    event["rates_b64"] = base64.b64encode(rates_val).decode("ascii")
                if dsset_val:
                    event["dsset_b64"] = base64.b64encode(dsset_val).decode("ascii")
                if tim_val:
                    event["tim_b64"] = base64.b64encode(tim_val).decode("ascii")
                if erp_val:
                    event["erp_b64"] = base64.b64encode(erp_val).decode("ascii")
                if esr_val:
                    event["esr_b64"] = base64.b64encode(esr_val).decode("ascii")

                REPLAY_FILE.write(json.dumps(event) + "\n")
                REPLAY_FILE.flush()

            print("-" * 50)

    except Exception as e:
        import traceback
        print(f"    - Error parsing payload: {type(e).__name__}: {e}")
        traceback.print_exc()


def main():
    from scapy.all import sniff, wrpcap

    parser = argparse.ArgumentParser(description="Drone Remote ID Wi-Fi Sniffer")
    parser.add_argument("--interface", default="wlan1", help="Wi-Fi Interface (default: wlan1)")
    parser.add_argument("--channel", type=int, default=6, help="Wi-Fi Channel to scan (default: 6)")
    parser.add_argument("--replay-out", help="Optional output JSONL file for use with replay_drones.py", default=None)
    parser.add_argument("--pcap-out", help="Optional output PCAP file to save raw packets", default=None)
    parser.add_argument("--no-setup", action="store_true", help="Skip bringing interface up/down (if already configured)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("[!] Warning: You usually need root privileges (sudo) to sniff Wi-Fi and configure monitor mode.")

    global REPLAY_FILE, PCAP_FILE

    if args.replay_out:
        REPLAY_FILE = open(args.replay_out, "w")
        print(f"[*] Replay events will be saved to {args.replay_out}")

    if args.pcap_out:
        PCAP_FILE = args.pcap_out
        wrpcap(PCAP_FILE, [])
        print(f"[*] Raw PCAP data will be saved to {args.pcap_out}")

    def cleanup(signum, frame):
        print("\n[*] Stopping capture...")
        if not args.no_setup:
            teardown_monitor_mode(args.interface)
        if REPLAY_FILE:
            REPLAY_FILE.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)

    try:
        if not args.no_setup:
            setup_monitor_mode(args.interface, args.channel)

        print(f"[*] Scanning for Wi-Fi Drone Remote ID Broadcasts on {args.interface} (Channel {args.channel})... (Press Ctrl+C to stop)")
        sniff(iface=args.interface, prn=process_packet, store=False)

    except Exception as e:
        print(f"[!] Error: {e}")
        cleanup(None, None)


if __name__ == "__main__":
    main()
