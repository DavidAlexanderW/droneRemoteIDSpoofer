#!/usr/bin/env python3
"""
measure_rf_density.py - Empirical Packet Density & Throughput Profiler

Measures real-world ambient RF packet density on Wi-Fi (802.11 monitor mode)
and Bluetooth (nRF sniffer) with NO preliminary filtering, taking into account
the tactical frequency hopping schedules used by the Remote ID scanner.

Features:
  • Wi-Fi Channel Hopping (2.4GHz & 5.8GHz social + non-social schedule)
  • Per-channel packet and throughput breakdown (Ch 6 vs Ch 149 vs non-social)
  • Bluetooth BLE5 / BLE4 mode-switching hopping profile
  • Fixed channel mode comparison (--fixed-channel <ch>)
  • Raw line-rate bandwidth (KB/sec, Mbps) & extrapolated volume (GB/day)
  • Frame classification: Management (Beacons, Probes, Action) vs Data vs Control vs Remote ID
"""

import os
import sys
import time
import socket
import select
import struct
import argparse
import threading
import subprocess
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

# ASTM OUI & APP Code
ASTM_OUI = b"\xFA\x0B\xBC"

# Import hopper and channel state from scanner.combined_rid_listener
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scanner.combined_rid_listener import (
    SharedChannelState,
    WifiChannelHopperThread,
    extract_radiotap_phy_info,
    get_freq_for_channel,
    get_channel_for_freq,
    SOCIAL_CHANNEL_2G,
    SOCIAL_CHANNEL_5G,
    NON_SOCIAL_CHANNELS_2G,
    NON_SOCIAL_CHANNELS_5G,
)


class WifiDensityMeter(threading.Thread):
    def __init__(
        self,
        interface: str,
        channel_state: Optional[SharedChannelState] = None,
        hopper_thread: Optional[WifiChannelHopperThread] = None,
        fixed_channel: Optional[int] = None,
    ):
        super().__init__(name="WifiDensityMeter", daemon=True)
        self.interface = interface
        self.channel_state = channel_state
        self.hopper_thread = hopper_thread
        self.fixed_channel = fixed_channel
        self.running = False
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()

        # Cumulative totals
        self.total_packets = 0
        self.total_bytes = 0
        self.frame_types = {
            "management": 0,
            "data": 0,
            "control": 0,
            "other": 0,
        }
        self.mgmt_subtypes = {
            "beacon": 0,
            "probe_req": 0,
            "probe_resp": 0,
            "action": 0,
            "other_mgmt": 0,
        }
        self.remote_id_packets = 0
        self.remote_id_bytes = 0

        # Per-channel attribution {ch: {"packets": count, "bytes": total_bytes}}
        self.channel_stats: Dict[int, Dict[str, int]] = {}

        # 1-second rate sliding window
        self.sec_packets = 0
        self.sec_bytes = 0
        self.history_rates_pps: List[float] = []
        self.history_rates_kbps: List[float] = []

    def run(self):
        self.running = True
        try:
            self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
            self.sock.bind((self.interface, 0))
        except Exception as e:
            sys.stderr.write(f"[-] Failed to bind raw socket on {self.interface}: {e}. (Root / sudo required)\n")
            self.running = False
            return

        last_tick = time.time()

        while self.running:
            try:
                rlist, _, _ = select.select([self.sock], [], [], 0.05)
                now = time.time()

                if rlist:
                    frame = self.sock.recv(4096)
                    flen = len(frame)
                    if flen < 24:
                        continue

                    # Radiotap header offset
                    rt_len = struct.unpack('<H', frame[2:4])[0] if (flen >= 4 and frame[0] == 0x00) else 0
                    if flen < rt_len + 2:
                        continue

                    fc = frame[rt_len]
                    f_type = (fc >> 2) & 0x03
                    f_subtype = (fc >> 4) & 0x0F

                    # Determine active channel from phy info / hopping state
                    cur_ch = self.fixed_channel or 6
                    if self.channel_state:
                        phy_info = extract_radiotap_phy_info(frame)
                        resolved_ch, _, _ = self.channel_state.resolve_channel(now, phy_info.get("frequency_mhz"))
                        cur_ch = resolved_ch
                    elif self.hopper_thread:
                        cur_ch = self.hopper_thread.current_channel

                    # Check for ASTM RID
                    is_rid = (ASTM_OUI in frame) or (f_type == 0 and f_subtype in (13, 14) and flen > rt_len + 24 and frame[rt_len] in (0xD0, 0xE0))

                    with self.lock:
                        self.total_packets += 1
                        self.total_bytes += flen
                        self.sec_packets += 1
                        self.sec_bytes += flen

                        # Channel attribution
                        if cur_ch not in self.channel_stats:
                            self.channel_stats[cur_ch] = {"packets": 0, "bytes": 0, "mgmt": 0, "data": 0, "rid": 0}
                        c_stat = self.channel_stats[cur_ch]
                        c_stat["packets"] += 1
                        c_stat["bytes"] += flen

                        if is_rid:
                            self.remote_id_packets += 1
                            self.remote_id_bytes += flen
                            c_stat["rid"] += 1

                        if f_type == 0:  # Management
                            self.frame_types["management"] += 1
                            c_stat["mgmt"] += 1
                            if f_subtype == 8:
                                self.mgmt_subtypes["beacon"] += 1
                            elif f_subtype == 4:
                                self.mgmt_subtypes["probe_req"] += 1
                            elif f_subtype == 5:
                                self.mgmt_subtypes["probe_resp"] += 1
                            elif f_subtype == 13:
                                self.mgmt_subtypes["action"] += 1
                            else:
                                self.mgmt_subtypes["other_mgmt"] += 1
                        elif f_type == 1:  # Control
                            self.frame_types["control"] += 1
                        elif f_type == 2:  # Data
                            self.frame_types["data"] += 1
                            c_stat["data"] += 1
                        else:
                            self.frame_types["other"] += 1

                # Every 1 second, snapshot rates
                if now - last_tick >= 1.0:
                    with self.lock:
                        pps = self.sec_packets / (now - last_tick)
                        kbps = (self.sec_bytes / 1024.0) / (now - last_tick)
                        self.history_rates_pps.append(pps)
                        self.history_rates_kbps.append(kbps)
                        self.sec_packets = 0
                        self.sec_bytes = 0
                    last_tick = now

            except Exception:
                if not self.running:
                    break

        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def stop(self):
        self.running = False


class BleDensityMeter(threading.Thread):
    def __init__(self, nrf_port: Optional[str] = None, ble_mode: str = "hop"):
        super().__init__(name="BleDensityMeter", daemon=True)
        self.nrf_port = nrf_port
        self.ble_mode = ble_mode
        self.running = False
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None

        self.total_packets = 0
        self.total_bytes = 0
        self.remote_id_packets = 0
        self.remote_id_bytes = 0
        self.pdu_types: Dict[str, int] = {}
        self.mode_stats: Dict[str, Dict[str, int]] = {"bt5": {"packets": 0, "bytes": 0}, "bt4": {"packets": 0, "bytes": 0}}

        self.sec_packets = 0
        self.sec_bytes = 0
        self.history_rates_pps: List[float] = []
        self.history_rates_kbps: List[float] = []

    def run(self):
        self.running = True
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate_paths = [
            os.path.join(script_dir, "sniffers", "nrf_bt_sniffer_json.py"),
            os.path.join(script_dir, "nrf_bt_sniffer_json.py"),
        ]
        nrf_script = next((p for p in candidate_paths if os.path.exists(p)), None)
        if not nrf_script:
            sys.stderr.write("[-] Could not find nrf_bt_sniffer_json.py for BLE density measurement.\n")
            self.running = False
            return

        active_port = self.nrf_port
        if not active_port:
            for p in ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"]:
                if os.path.exists(p):
                    active_port = p
                    break

        if not active_port:
            sys.stderr.write("[-] No nRF BLE sniffer device found on /dev/ttyACM* or /dev/ttyUSB*.\n")
            self.running = False
            return

        cmd = [
            sys.executable, nrf_script,
            "--nrf-port", active_port,
            "--ble-mode", self.ble_mode,
            "--bt5-dwell", "5.0",
            "--bt4-dwell", "1.0",
            # NOTE: No --only-rid flag, we measure all ambient BLE advertising!
        ]

        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            last_tick = time.time()

            while self.running and self.proc.poll() is None:
                line = self.proc.stdout.readline()
                if not line:
                    continue
                line_str = line.strip()
                if not line_str.startswith("{"):
                    continue

                now = time.time()
                try:
                    import json
                    record = json.loads(line_str)
                    raw_len = int(record.get("raw_length", 32))
                    pdu = str(record.get("pdu_type", "UNKNOWN"))
                    is_rid = bool(record.get("remote_id"))
                    cur_mode = str(record.get("active_ble_mode", "bt5")).lower()
                    mode_key = "bt5" if ("5" in cur_mode or "ext" in cur_mode or "EXT" in pdu or "AUX" in pdu) else "bt4"

                    with self.lock:
                        self.total_packets += 1
                        self.total_bytes += raw_len
                        self.sec_packets += 1
                        self.sec_bytes += raw_len
                        self.pdu_types[pdu] = self.pdu_types.get(pdu, 0) + 1

                        if mode_key not in self.mode_stats:
                            self.mode_stats[mode_key] = {"packets": 0, "bytes": 0}
                        self.mode_stats[mode_key]["packets"] += 1
                        self.mode_stats[mode_key]["bytes"] += raw_len

                        if is_rid:
                            self.remote_id_packets += 1
                            self.remote_id_bytes += raw_len

                except Exception:
                    pass

                if now - last_tick >= 1.0:
                    with self.lock:
                        pps = self.sec_packets / (now - last_tick)
                        kbps = (self.sec_bytes / 1024.0) / (now - last_tick)
                        self.history_rates_pps.append(pps)
                        self.history_rates_kbps.append(kbps)
                        self.sec_packets = 0
                        self.sec_bytes = 0
                    last_tick = now

        except Exception as e:
            sys.stderr.write(f"[-] Error in BLE density meter: {e}\n")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


def main():
    parser = argparse.ArgumentParser(
        description="RF Packet Density & Throughput Profiler (with Frequency Hopping)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--wifi-iface", default=None, help="Wi-Fi monitor mode interface (e.g. wlan1, wlan0mon)")
    parser.add_argument("--nrf-port", default=None, help="Nordic nRF BLE sniffer serial port (e.g. /dev/ttyACM0)")
    parser.add_argument("--duration", "-d", type=int, default=30, help="Test duration in seconds (0 = run indefinitely until Ctrl+C)")
    parser.add_argument("--fixed-channel", type=int, default=None, help="Lock Wi-Fi to a single channel (disables hopping, e.g. --fixed-channel 6)")
    parser.add_argument("--social-dwell-ms", type=int, default=1000, help="Hopper dwell time on social channels (ms)")
    parser.add_argument("--non-social-dwell-ms", type=int, default=200, help="Hopper dwell time on 2.4GHz non-social channels (ms)")
    parser.add_argument("--non-social-dwell-5g-ms", type=int, default=250, help="Hopper dwell time on 5.8GHz non-social channels (ms)")
    parser.add_argument("--ble-mode", choices=["hop", "all", "extended", "legacy"], default="hop", help="BLE mode switching strategy (hop: 5s BT5 / 1s BT4)")
    parser.add_argument("--no-wifi", action="store_true", help="Skip Wi-Fi density measurement")
    parser.add_argument("--no-ble", action="store_true", help="Skip BLE density measurement")

    args = parser.parse_args()

    wifi_meter: Optional[WifiDensityMeter] = None
    ble_meter: Optional[BleDensityMeter] = None
    hopper_thread: Optional[WifiChannelHopperThread] = None
    channel_state: Optional[SharedChannelState] = None

    if not args.no_wifi:
        if not args.wifi_iface:
            # 1. Check iw dev for an active monitor interface
            try:
                out = subprocess.check_output(["iw", "dev"], text=True, stderr=subprocess.DEVNULL)
                cur_iface = None
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("Interface "):
                        cur_iface = line.split()[1]
                    elif "type monitor" in line and cur_iface:
                        args.wifi_iface = cur_iface
                        break
            except Exception:
                pass

        if not args.wifi_iface:
            # 2. Check scanner_config.json
            for p in ["scanner/scanner_config.json", "scanner_config.json"]:
                if os.path.exists(p):
                    try:
                        import json
                        with open(p, "r") as f:
                            c = json.load(f)
                            if c.get("wifi_interface"):
                                args.wifi_iface = c["wifi_interface"]
                                break
                    except Exception:
                        pass

        if not args.wifi_iface:
            # 3. Check ip link for mon / wlan
            try:
                out = subprocess.check_output(["ip", "link"], text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    for token in line.split():
                        if "mon" in token:
                            clean = token.replace(":", "")
                            args.wifi_iface = clean
                            break
            except Exception:
                pass

        if args.wifi_iface:
            if args.fixed_channel is None:
                # Active Hopping Mode (Production Strategy)
                channel_state = SharedChannelState(initial_channel=SOCIAL_CHANNEL_2G, drain_retention_ms=5.0)
                hopper_thread = WifiChannelHopperThread(
                    interface=args.wifi_iface,
                    channel_state=channel_state,
                    non_social_ratio_k=1,
                    social_dwell_ms=args.social_dwell_ms,
                    non_social_dwell_ms=args.non_social_dwell_ms,
                    non_social_dwell_5g_ms=args.non_social_dwell_5g_ms,
                )
                hopper_thread.start()

            wifi_meter = WifiDensityMeter(
                interface=args.wifi_iface,
                channel_state=channel_state,
                hopper_thread=hopper_thread,
                fixed_channel=args.fixed_channel,
            )
            wifi_meter.start()
        else:
            print("[!] No Wi-Fi monitor interface specified or detected. Skipping Wi-Fi. (Use --wifi-iface <name>)")

    if not args.no_ble:
        ble_meter = BleDensityMeter(args.nrf_port, ble_mode=args.ble_mode)
        ble_meter.start()

    if not wifi_meter and not ble_meter:
        print("[-] Neither Wi-Fi nor BLE meter could be started. Exiting.")
        sys.exit(1)

    hop_mode_str = f"LOCKED to Channel {args.fixed_channel}" if args.fixed_channel else f"ACTIVE HOPPING (Ch 6 / 149 social + 18 channels, {args.social_dwell_ms}ms/{args.non_social_dwell_ms}ms)"
    ble_mode_str = f"ACTIVE MODE HOPPING (BT5: 5.0s / BT4: 1.0s)" if args.ble_mode == "hop" else f"Mode: {args.ble_mode.upper()}"

    print("\n" + "=" * 75)
    print("📡 RF PACKET DENSITY & CAPTURE PROFILER (FREQUENCY HOPPING ENABLED)")
    print(f"  • Wi-Fi Interface : {args.wifi_iface or 'Disabled'} [{hop_mode_str}]")
    print(f"  • BLE Sniffer     : {ble_meter.nrf_port or 'Auto' if ble_meter else 'Disabled'} [{ble_mode_str}]")
    print(f"  • Test Duration   : {args.duration}s (or press Ctrl+C to stop early)")
    print("=" * 75 + "\n")

    start_time = time.time()
    try:
        while True:
            time.sleep(1.0)
            elapsed = time.time() - start_time

            wifi_str = "N/A"
            cur_ch_str = ""
            if wifi_meter and wifi_meter.is_alive():
                with wifi_meter.lock:
                    w_pps = wifi_meter.history_rates_pps[-1] if wifi_meter.history_rates_pps else 0
                    w_kbps = wifi_meter.history_rates_kbps[-1] if wifi_meter.history_rates_kbps else 0
                    w_mbps = (w_kbps * 8) / 1000.0
                    cur_ch = hopper_thread.current_channel if hopper_thread else (args.fixed_channel or 6)
                    cur_ch_str = f"[Ch {cur_ch:>3}] "
                    wifi_str = f"{cur_ch_str}{w_pps:>4.0f} pkts/s | {w_kbps:>5.1f} KB/s ({w_mbps:>4.2f} Mbps)"

            ble_str = "N/A"
            if ble_meter and ble_meter.is_alive():
                with ble_meter.lock:
                    b_pps = ble_meter.history_rates_pps[-1] if ble_meter.history_rates_pps else 0
                    b_kbps = ble_meter.history_rates_kbps[-1] if ble_meter.history_rates_kbps else 0
                    ble_str = f"{b_pps:>4.0f} pkts/s | {b_kbps:>5.1f} KB/s"

            sys.stdout.write(f"\r[{elapsed:>3.0f}s] Wi-Fi: {wifi_str}  │  BLE: {ble_str}   ")
            sys.stdout.flush()

            if args.duration > 0 and elapsed >= args.duration:
                break

    except KeyboardInterrupt:
        print("\n\n[*] Stopped by user (Ctrl+C). Compiling statistics...")
    finally:
        if hopper_thread:
            hopper_thread.stop()
        if wifi_meter:
            wifi_meter.stop()
        if ble_meter:
            ble_meter.stop()

    actual_duration = max(1.0, time.time() - start_time)

    print("\n\n" + "=" * 75)
    print(f"📊 EMPIRICAL RF CAPTURE PROFILE ({actual_duration:.1f}s sample with Hopping)")
    print("=" * 75)

    # 1. Wi-Fi Results
    if wifi_meter:
        with wifi_meter.lock:
            w_pkts = wifi_meter.total_packets
            w_bytes = wifi_meter.total_bytes
            w_pps_avg = w_pkts / actual_duration
            w_kbps_avg = (w_bytes / 1024.0) / actual_duration
            w_mbps_avg = (w_kbps_avg * 8) / 1000.0
            w_pps_peak = max(wifi_meter.history_rates_pps) if wifi_meter.history_rates_pps else w_pps_avg
            w_kbps_peak = max(wifi_meter.history_rates_kbps) if wifi_meter.history_rates_kbps else w_kbps_avg

            bytes_per_hour = w_bytes * (3600.0 / actual_duration)
            gb_per_day = (bytes_per_hour * 24.0) / (1024.0 ** 3)

            mgmt_pct = (wifi_meter.frame_types['management'] / w_pkts * 100.0) if w_pkts else 0
            data_pct = (wifi_meter.frame_types['data'] / w_pkts * 100.0) if w_pkts else 0
            ctrl_pct = (wifi_meter.frame_types['control'] / w_pkts * 100.0) if w_pkts else 0
            rid_pct = (wifi_meter.remote_id_packets / w_pkts * 100.0) if w_pkts else 0

        print(f"\n📶 WI-FI PROFILE ({'Hopping Across 18 Channels' if not args.fixed_channel else f'Locked to Channel {args.fixed_channel}'}):")
        print(f"  • Total Packets Captured    : {w_pkts:,} frames")
        print(f"  • Total Raw Data Received   : {w_bytes / (1024*1024):.2f} MB")
        print(f"  • Aggregate Packet Rate     : {w_pps_avg:.1f} pkts/sec (Peak: {w_pps_peak:.1f} pkts/sec)")
        print(f"  • Aggregate Bandwidth       : {w_kbps_avg:.1f} KB/s ({w_mbps_avg:.2f} Mbps) | Peak: {w_kbps_peak:.1f} KB/s")
        print(f"  • Extrapolated Daily Volume : \033[1;33m{gb_per_day:.2f} GB / day\033[0m ({bytes_per_hour / (1024*1024):.1f} MB / hour)")
        print("  • Frame Type Distribution:")
        print(f"      - Management Frames     : {wifi_meter.frame_types['management']:,} ({mgmt_pct:.1f}%) [Beacons: {wifi_meter.mgmt_subtypes['beacon']:,}, Probes: {wifi_meter.mgmt_subtypes['probe_req'] + wifi_meter.mgmt_subtypes['probe_resp']:,}, Action: {wifi_meter.mgmt_subtypes['action']:,}]")
        print(f"      - Data Frames           : {wifi_meter.frame_types['data']:,} ({data_pct:.1f}%)")
        print(f"      - Control Frames        : {wifi_meter.frame_types['control']:,} ({ctrl_pct:.1f}%)")
        print(f"      - Remote ID Frames      : {wifi_meter.remote_id_packets:,} ({rid_pct:.2f}%)")

        # Per-Channel breakdown (Crucial for hopping analysis!)
        if wifi_meter.channel_stats and not args.fixed_channel:
            print("\n  • Per-Channel Packet Distribution (Social vs Non-Social Dwells):")
            def _ch_key(c):
                try: return (0 if c < 30 else 1, int(c))
                except: return (2, str(c))
            for ch in sorted(wifi_meter.channel_stats.keys(), key=_ch_key):
                c_data = wifi_meter.channel_stats[ch]
                ch_tag = "(2.4G Social)" if ch == 6 else ("(5.8G Social)" if ch == 149 else "(Non-Social)")
                ch_mb = c_data['bytes'] / (1024 * 1024)
                print(f"      - Channel {ch:<4} {ch_tag:<14}: {c_data['packets']:>5,} pkts ({ch_mb:>5.2f} MB) | Mgmt: {c_data['mgmt']:>5,} | Data: {c_data['data']:>5,} | RID: {c_data['rid']}")

    # 2. BLE Results
    if ble_meter:
        with ble_meter.lock:
            b_pkts = ble_meter.total_packets
            b_bytes = ble_meter.total_bytes
            b_pps_avg = b_pkts / actual_duration
            b_kbps_avg = (b_bytes / 1024.0) / actual_duration
            b_pps_peak = max(ble_meter.history_rates_pps) if ble_meter.history_rates_pps else b_pps_avg
            b_kbps_peak = max(ble_meter.history_rates_kbps) if ble_meter.history_rates_kbps else b_kbps_avg

            b_bytes_per_hour = b_bytes * (3600.0 / actual_duration)
            b_mb_per_day = (b_bytes_per_hour * 24.0) / (1024.0 * 1024.0)

        print(f"\nᛒ BLUETOOTH LE PROFILE ({'Mode-Switching Hopping: 5s BT5 / 1s BT4' if args.ble_mode == 'hop' else args.ble_mode}):")
        print(f"  • Total Packets Captured    : {b_pkts:,} advertisements")
        print(f"  • Total Raw Data Received   : {b_bytes / 1024:.1f} KB")
        print(f"  • Average Packet Rate       : {b_pps_avg:.1f} pkts/sec (Peak: {b_pps_peak:.1f} pkts/sec)")
        print(f"  • Average Bandwidth         : {b_kbps_avg:.1f} KB/s")
        print(f"  • Extrapolated Daily Volume : \033[1;32m{b_mb_per_day:.1f} MB / day\033[0m ({b_bytes_per_hour / 1024:.1f} KB / hour)")
        if ble_meter.mode_stats:
            print("  • Mode Breakdown:")
            for m_key, m_val in ble_meter.mode_stats.items():
                print(f"      - {m_key.upper():<6}: {m_val['packets']:,} pkts ({m_val['bytes']/1024:.1f} KB)")
        if ble_meter.pdu_types:
            print("  • PDU Types Encountered:")
            for pdu, count in sorted(ble_meter.pdu_types.items(), key=lambda x: x[1], reverse=True)[:5]:
                print(f"      - {pdu:<24}: {count:,}")

    # 3. Decision Matrix
    print("\n" + "-" * 75)
    print("💡 ARCHITECTURAL DECISION MATRIX FOR YOUR DEPLOYMENT")
    print("-" * 75)
    if ble_meter:
        print(f"  1. Bluetooth: ~{b_mb_per_day:.0f} MB/day ({b_kbps_avg:.1f} KB/s)")
        print(f"     ✅ Safe to stream 100% unfiltered over any connection.")
    if wifi_meter:
        print(f"  2. Wi-Fi (with Active Hopping): ~{gb_per_day:.1f} GB/day ({w_mbps_avg:.2f} Mbps continuous)")
        if gb_per_day > 10.0:
            mgmt_gb = (gb_per_day * mgmt_pct) / 100.0
            print(f"     ⚠️  Full unfiltered Wi-Fi generates significant data volume ({gb_per_day:.1f} GB/day).")
            print(f"        Filtering out bulky Data frames ({data_pct:.1f}%) drops bandwidth to ~{mgmt_gb:.1f} GB/day.")
        else:
            print(f"     ✅ Low volume! Full unfiltered Wi-Fi capture is completely feasible on your backhaul.")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    main()
