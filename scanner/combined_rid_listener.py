#!/usr/bin/env python3
"""
Combined Bluetooth (nRF UART) and Wi-Fi Drone Remote ID (RID) Listener & Logger
Compliant with ASTM F3411-19 / ASTM F3411-22 / ASD-STAN OpenDroneID standards.

Architecture:
- Thread 1: BLE Sniffer Thread (BleNrfSnifferThread over UART for BLE 4/5 RID packets)
- Thread 2: Wi-Fi Channel Hopper Thread (WifiChannelHopperThread multi-band schedule)
- Thread 3: Wi-Fi Sniffer Thread (WifiSnifferThread AF_PACKET raw socket capturing Beacons & NAN)
- Logging Pipeline: UnifiedTelemetryLogger managing console UI, SQLite encounters, and forwarding.

Note: All components are modularized in scanner.* submodules. This module acts as the
CLI entrypoint and a backward-compatible re-export façade.
"""

import argparse
import logging
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# Setup sys.path for direct script execution or package import
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
scanner_dir = os.path.abspath(os.path.dirname(__file__))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
if scanner_dir not in sys.path:
    sys.path.insert(0, scanner_dir)

# Submodule Imports & Backward-Compatible Re-exports
from scanner.channel_state import (
    NON_SOCIAL_CHANNELS_2G,
    NON_SOCIAL_CHANNELS_5G,
    SOCIAL_CHANNEL_2G,
    SOCIAL_CHANNEL_5G,
    SharedChannelState,
    get_band_for_channel,
    get_channel_for_freq,
    get_freq_for_channel,
)
from scanner.radiotap import extract_radiotap_phy_info, extract_radiotap_rssi
from scanner.timestamp_utils import (
    DEFAULT_MAX_CLOCK_SKEW_S,
    DEFAULT_MIN_VALID_TIMESTAMP,
    is_valid_timestamp,
    parse_iso_timestamp,
    resolve_reception_timestamp,
)
from scanner.encounter_tracker import EncounterTracker
from scanner.rehydrator import rehydrate_db_from_jsonl
from scanner.sniffers.wifi_sniffer import (
    WifiChannelHopperThread,
    WifiSnifferThread,
    restore_managed_mode,
    setup_monitor_mode,
)
from scanner.sniffers.bt_sniffer import BleNrfSnifferThread
from scanner.telemetry_logger import (
    C_BLUE,
    C_BOLD,
    C_CYAN,
    C_GRAY,
    C_GREEN,
    C_MAGENTA,
    C_RED,
    C_RESET,
    C_WHITE,
    C_YELLOW,
    UnifiedTelemetryLogger,
)
from scanner.scanner_config import load_scanner_config, save_scanner_config
from scanner.drone_models import infer_drone_model
from scanner.forwarder import CentralStreamForwarder
from scanner.pcap_logger import DailyNodePcapLogger, DLT_IEEE802_11_RADIO, DLT_NORDIC_BLE
from scanner.db import (
    get_db_connection as db_get_connection,
    haversine_m,
    init_encounters_db,
    is_better_operator_id,
    is_better_serial,
    is_fuzzy_operator_match,
    is_fuzzy_serial_match,
    merge_sequential_encounters,
    reconcile_stale_encounters,
    sanitize_operator_id,
    sanitize_serial,
    touch_receiver_node_heartbeat,
    update_receiver_node_position,
    upsert_receiver_node,
)
from scanner.parser import (
    APP_CODE_RID,
    ASTM_OUI,
    AUTH_TYPE_NAMES,
    BLE_RID_UUID,
    CLASSIFICATION_TYPE_NAMES,
    DESC_TYPE_NAMES,
    EU_CATEGORY_NAMES,
    EU_CLASS_NAMES,
    HEIGHT_TYPE_NAMES,
    HORIZ_ACCURACY_NAMES,
    ID_TYPE_NAMES,
    MSG_TYPE_NAMES,
    OPENDRONEID_EPOCH_2019,
    OPERATOR_LOCATION_TYPE_NAMES,
    PROTO_VERSION_NAMES,
    SPEED_ACCURACY_NAMES,
    STATUS_NAMES,
    TIMESTAMP_ACCURACY_NAMES,
    UA_TYPE_NAMES,
    VERT_ACCURACY_NAMES,
    decode_astm_message,
    parse_astm_payload,
    sanitize_ascii_string,
)

from scanner.pcap_streamer import BinaryPcapStreamer

__all__ = [
    "SharedChannelState",
    "WifiChannelHopperThread",
    "WifiSnifferThread",
    "BleNrfSnifferThread",
    "UnifiedTelemetryLogger",
    "EncounterTracker",
    "rehydrate_db_from_jsonl",
    "extract_radiotap_phy_info",
    "extract_radiotap_rssi",
    "get_band_for_channel",
    "get_freq_for_channel",
    "get_channel_for_freq",
    "SOCIAL_CHANNEL_2G",
    "NON_SOCIAL_CHANNELS_2G",
    "SOCIAL_CHANNEL_5G",
    "NON_SOCIAL_CHANNELS_5G",
    "setup_monitor_mode",
    "restore_managed_mode",
    "resolve_reception_timestamp",
    "is_valid_timestamp",
    "parse_iso_timestamp",
    "decode_astm_message",
    "parse_astm_payload",
    "main",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CombinedRIDListener")


def main():
    parser = argparse.ArgumentParser(
        description="Combined Bluetooth (nRF UART) and Wi-Fi Drone Remote ID Listener and Console Logger",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Hardware & Interfaces
    parser.add_argument("--wifi-iface", "-i", default=None, help="Wi-Fi monitor-mode interface (e.g., wlan1)")
    parser.add_argument(
        "--nrf-port", "-p", default=None, help="nRF Sniffer UART serial port (e.g., /dev/ttyACM0). Auto-detected if omitted."
    )
    parser.add_argument("--rx-pcap", help="Replay pre-captured BLE pcap file for offline verification")

    # Subsystem toggles
    parser.add_argument("--no-wifi", action="store_true", help="Disable Wi-Fi sniffing and channel hopping")
    parser.add_argument("--no-ble", action="store_true", help="Disable Bluetooth sniffing")
    parser.add_argument("--no-wifi-setup", action="store_true", help="Skip bringing Wi-Fi interface down/up into monitor mode")

    # Hopping Schedule Configuration
    parser.add_argument(
        "--wifi-channel",
        "--channel",
        "-c",
        type=int,
        default=None,
        help="Lock Wi-Fi sniffer to a single fixed channel (e.g. 6 or 149) and disable hopping",
    )
    parser.add_argument(
        "--no-hop", action="store_true", help="Disable Wi-Fi channel hopping (listen only on initial channel)"
    )
    parser.add_argument(
        "--non-social-ratio",
        "-k",
        type=int,
        default=1,
        help="Non-social channel ratio multiplier k (cycles 2k non-social on 2.4GHz for every k on 5.8GHz)",
    )
    parser.add_argument("--social-dwell-ms", type=int, default=1000, help="Social channel dwell time in milliseconds (1 Hz)")
    parser.add_argument(
        "--non-social-dwell-ms",
        type=int,
        default=200,
        help="2.4 GHz non-social channel dwell time in milliseconds (default: 200ms)",
    )
    parser.add_argument(
        "--non-social-dwell-5g-ms",
        type=int,
        default=250,
        help="5.8 GHz non-social channel dwell time in milliseconds (default: 250ms extended dwell to compensate for mixed/negative PLL lead times)",
    )
    parser.add_argument(
        "--drain-retention-ms",
        type=float,
        default=5.0,
        help="Buffer drain retention window in ms for previous channel packet attribution (default: 5.0ms)",
    )

    # Distributed Hub & Forwarding Options
    cfg = load_scanner_config()
    default_node_id = cfg.get("node_id", "sensor-node-01")
    default_hub_url = cfg.get("hub_ws_url")
    default_spool_dir = cfg.get("spool_dir", "spool")
    default_max_ram = int(cfg.get("max_ram_queue", 10000))
    default_ble_mode = cfg.get("ble_mode", "hop")
    default_ble_bt5_dwell = float(cfg.get("ble_bt5_dwell_s", 5.0))
    default_ble_bt4_dwell = float(cfg.get("ble_bt4_dwell_s", 1.0))

    # BLE Options
    parser.add_argument("--coded", action="store_true", help="Enable Bluetooth 5 Long Range (LE Coded PHY) scanning")
    parser.add_argument(
        "--ble-mode",
        choices=["hop", "extended", "legacy", "all"],
        default=default_ble_mode,
        help="BLE advertisement filter mode (default: hop)",
    )
    parser.add_argument(
        "--ble-bt5-dwell",
        type=float,
        default=default_ble_bt5_dwell,
        help="BT5 Extended / Coded dwell time in seconds (default: 5.0s)",
    )
    parser.add_argument(
        "--ble-bt4-dwell",
        type=float,
        default=default_ble_bt4_dwell,
        help="BT4 Legacy dwell time in seconds (default: 1.0s)",
    )

    parser.add_argument(
        "--scanner-config",
        default=None,
        help="Path to JSON configuration file for scanner station parameters (default: scanner/scanner_config.json)",
    )
    parser.add_argument(
        "--hub-url", default=default_hub_url, help="Central Ingestion Hub WebSocket URL (e.g. ws://hub-ip:8000/stream/node)"
    )
    parser.add_argument("--node-id", default=default_node_id, help="Sensor node identifier")
    parser.add_argument(
        "--standalone", action="store_true", help="Force standalone mode (disables forwarding, enables local SQLite/JSONL)"
    )
    parser.add_argument("--spool-dir", default=default_spool_dir, help="Directory for temporary spool files during network outages")
    parser.add_argument(
        "--max-ram-queue", type=int, default=default_max_ram, help="Max in-memory RAM queue size before spilling to disk"
    )

    # Storage & Logging
    parser.add_argument(
        "--db-file",
        default="rid_detections.db",
        help="SQLite database path for 5-minute flight encounter records (set empty '' to disable)",
    )
    parser.add_argument(
        "--encounter-timeout-s",
        type=float,
        default=300.0,
        help="Flight encounter timeout in seconds (default 300s / 5 minutes)",
    )
    parser.add_argument(
        "--persist-interval",
        type=float,
        default=2.0,
        help="Maximum frequency in seconds to persist active encounters to SQLite (default: 2.0s)",
    )
    parser.add_argument("--log-jsonl", default=None, help="Optional replay-compatible JSONL log file path")
    parser.add_argument("--rotate-daily", action="store_true", help="Automatically split JSONL log file daily (<name>_YYYYMMDD.jsonl)")
    parser.add_argument(
        "--log-pcap",
        nargs="?",
        const="pcaps",
        default=None,
        help="Enable daily PCAP logging with optional output directory or path prefix (default: pcaps)",
    )
    parser.add_argument(
        "--pcap-dir", default=None, help="Directory to store daily PCAP log files (default: pcaps if --log-pcap enabled)"
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Quiet / daemon mode: suppress per-packet console banner and print periodic heartbeat status",
    )
    parser.add_argument(
        "--sync-only", action="store_true", help="Synchronize pending spool files and historical backlog to Central Hub and exit"
    )
    parser.add_argument(
        "--rehydrate",
        action="store_true",
        help="Retroactively re-parse all raw base64 ASTM messages from rid_packets_*.jsonl files and update the SQLite database",
    )

    args = parser.parse_args()

    if args.scanner_config:
        try:
            cfg = load_scanner_config(args.scanner_config)
        except Exception as e:
            logger.warning(f"[!] Warning: Failed to parse scanner config '{args.scanner_config}': {e}")
            logger.warning("[!] Scanner is continuing in Standalone Local Mode with default parameters.")
            cfg = {}
        if args.hub_url == default_hub_url:
            args.hub_url = cfg.get("hub_ws_url")
        if args.node_id == default_node_id:
            args.node_id = cfg.get("node_id", default_node_id)
        if args.spool_dir == default_spool_dir:
            args.spool_dir = cfg.get("spool_dir", default_spool_dir)
        if args.max_ram_queue == default_max_ram:
            args.max_ram_queue = int(cfg.get("max_ram_queue", default_max_ram))
        if args.ble_mode == default_ble_mode:
            args.ble_mode = cfg.get("ble_mode", default_ble_mode)
        if args.ble_bt5_dwell == default_ble_bt5_dwell:
            args.ble_bt5_dwell = float(cfg.get("ble_bt5_dwell_s", default_ble_bt5_dwell))
        if args.ble_bt4_dwell == default_ble_bt4_dwell:
            args.ble_bt4_dwell = float(cfg.get("ble_bt4_dwell_s", default_ble_bt4_dwell))
        if not args.log_pcap and cfg.get("log_pcap"):
            args.log_pcap = "pcaps"
        if not args.pcap_dir and (cfg.get("pcap_dir") or cfg.get("log_pcap_dir")):
            args.pcap_dir = cfg.get("pcap_dir") or cfg.get("log_pcap_dir")

    if not args.no_wifi and not args.wifi_iface:
        args.no_wifi = True

    if os.geteuid() != 0 and not args.no_wifi:
        logger.warning("[!] Warning: Root privileges (sudo) are recommended for Wi-Fi monitor mode and raw socket capture.")

    initial_wifi_ch = args.wifi_channel if args.wifi_channel is not None else SOCIAL_CHANNEL_2G

    if args.rehydrate:
        rehydrated = rehydrate_db_from_jsonl(
            db_path=args.db_file if args.db_file else "rid_detections.db",
            node_id=args.node_id,
        )
        print(
            f"{C_GREEN}[+] Database rehydration complete: {rehydrated} encounter(s) updated in "
            f"{args.db_file or 'rid_detections.db'}.{C_RESET}"
        )
        if not args.hub_url and not args.wifi_iface and not args.nrf_port:
            return

    # Configure Distributed Stream Forwarder or Standalone Mode
    is_hub_mode = bool(args.hub_url and not args.standalone)
    forwarder = None

    if is_hub_mode:
        if CentralStreamForwarder is not None:
            # Auto-detect historical backlog logs in working dir & repo dir (rid_packets_*.jsonl, rid_packets.jsonl)
            backlog_patterns = [
                os.path.join(os.getcwd(), "rid_packets*.jsonl"),
                os.path.join(os.getcwd(), "capture*.jsonl"),
            ]
            if os.path.abspath(os.getcwd()) != repo_root:
                backlog_patterns.append(os.path.join(repo_root, "rid_packets*.jsonl"))
                backlog_patterns.append(os.path.join(repo_root, "capture*.jsonl"))

            def on_position_updated_cb(lat: float, lon: float, alt: Optional[float] = None):
                if (
                    "logger_worker" in locals()
                    and logger_worker
                    and logger_worker.encounter_tracker
                    and logger_worker.encounter_tracker.db_path
                    and args.node_id
                ):
                    try:
                        conn_up = sqlite3.connect(logger_worker.encounter_tracker.db_path, timeout=10.0)
                        update_receiver_node_position(conn_up, args.node_id, lat, lon, alt)
                        conn_up.close()
                    except Exception as e:
                        logger.debug(f"Could not update local receiver node position in DB: {e}")

            forwarder = CentralStreamForwarder(
                hub_ws_url=args.hub_url,
                node_id=args.node_id,
                node_meta=cfg,
                config_path=args.scanner_config,
                on_position_updated=on_position_updated_cb,
                spool_dir=args.spool_dir,
                backlog_paths=backlog_patterns,
                max_ram_queue=args.max_ram_queue,
                quiet=args.quiet,
            )
            forwarder.start()
            logger.info(f"[*] Operational Mode: CENTRALIZED STREAMING -> Hub: {args.hub_url} | Node ID: {args.node_id}")

            if args.sync_only:
                logger.info("[*] --sync-only specified: Waiting for catch-up backlog upload to complete...")
                forwarder.catchup_complete.wait(timeout=120.0)
                time.sleep(1.0)
                forwarder.stop()
                logger.info("[+] Catch-up backlog synchronization complete. Exiting.")
                return
        else:
            logger.error("[-] CentralStreamForwarder module could not be loaded. Running standalone.")

        pcap_streamer = None
        has_wifi = not args.no_wifi
        has_ble = not args.no_ble

        if has_wifi or has_ble:
            pcap_streamer = BinaryPcapStreamer(
                hub_url=args.hub_url,
                node_id=args.node_id,
                quiet=args.quiet,
                enable_wifi=has_wifi,
                enable_ble=has_ble,
            )
            pcap_streamer.start()
            channels = []
            if has_wifi:
                channels.append("Wi-Fi")
            if has_ble:
                channels.append("BLE")
            logger.info(
                f"[*] Operational Mode: CONCURRENT BINARY PCAP STREAMING ({' + '.join(channels)}) -> Hub: {pcap_streamer.base_ws_url}"
            )
    else:
        pcap_streamer = None
        logger.info(
            f"[*] Operational Mode: STANDALONE LOCAL -> Encounters DB: {args.db_file or 'disabled'} (No Hub URL configured)"
        )

    if args.no_wifi and args.no_ble:
        if is_hub_mode and forwarder:
            logger.info(
                "[*] Radios disabled (--no-wifi --no-ble). Stream forwarder is running in background to drain spool/backlog."
            )
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                logger.info("[*] Stopping forwarder...")
                forwarder.stop()
                return
        else:
            logger.error("[-] Both Wi-Fi and BLE are disabled and no Hub URL configured. Nothing to do!")
            sys.exit(1)

    # Determine local storage paths: in hub mode, disable continuous local disk writes unless explicitly specified
    db_path_to_use = None
    if not is_hub_mode:
        db_path_to_use = args.db_file if args.db_file else None
    elif args.db_file and args.db_file != "rid_detections.db":
        db_path_to_use = args.db_file

    log_jsonl_to_use = args.log_jsonl if (not is_hub_mode or args.log_jsonl) else None
    log_pcap_to_use = (
        (args.log_pcap or args.pcap_dir) if (not is_hub_mode or args.log_pcap or args.pcap_dir) else None
    )
    pcap_dir_to_use = (
        (args.pcap_dir or (args.log_pcap if (args.log_pcap and os.path.isdir(args.log_pcap)) else None))
        if log_pcap_to_use
        else None
    )

    event_queue: queue.Queue = queue.Queue()
    channel_state = SharedChannelState(initial_channel=initial_wifi_ch, drain_retention_ms=args.drain_retention_ms)
    logger_worker = UnifiedTelemetryLogger(
        log_jsonl_path=log_jsonl_to_use,
        db_path=db_path_to_use,
        encounter_timeout_s=args.encounter_timeout_s,
        quiet=args.quiet,
        rotate_daily=args.rotate_daily,
        persist_interval_s=args.persist_interval,
        forwarder=forwarder,
        node_id=args.node_id,
        node_meta=cfg,
        log_pcap=log_pcap_to_use,
        pcap_dir=pcap_dir_to_use,
    )

    threads: List[threading.Thread] = []
    hopper_thread: Optional[WifiChannelHopperThread] = None
    wifi_thread: Optional[WifiSnifferThread] = None
    ble_thread: Optional[BleNrfSnifferThread] = None

    # Setup Wi-Fi
    if not args.no_wifi:
        if not args.no_wifi_setup:
            setup_monitor_mode(args.wifi_iface, initial_channel=initial_wifi_ch)

        if not args.no_hop and args.wifi_channel is None:
            hopper_thread = WifiChannelHopperThread(
                interface=args.wifi_iface,
                channel_state=channel_state,
                non_social_ratio_k=args.non_social_ratio,
                social_dwell_ms=args.social_dwell_ms,
                non_social_dwell_ms=args.non_social_dwell_ms,
                non_social_dwell_5g_ms=args.non_social_dwell_5g_ms,
            )
            threads.append(hopper_thread)
        else:
            logger.info(f"[*] Wi-Fi sniffer locked to fixed Channel {initial_wifi_ch} (hopping disabled).")

        wifi_thread = WifiSnifferThread(
            interface=args.wifi_iface,
            channel_state=channel_state,
            event_queue=event_queue,
            pcap_streamer=pcap_streamer,
        )
        threads.append(wifi_thread)

    # Setup BLE
    if not args.no_ble:
        nrf_port = args.nrf_port
        if not nrf_port and not args.rx_pcap:
            for candidate in ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"]:
                if os.path.exists(candidate):
                    nrf_port = candidate
                    logger.info(f"[*] Auto-detected nRF BLE sniffer on {candidate}")
                    break
        ble_thread = BleNrfSnifferThread(
            event_queue=event_queue,
            nrf_port=nrf_port,
            rx_pcap=args.rx_pcap,
            coded=args.coded,
            ble_mode=args.ble_mode,
            bt5_dwell_s=args.ble_bt5_dwell,
            bt4_dwell_s=args.ble_bt4_dwell,
            pcap_streamer=pcap_streamer,
        )
        threads.append(ble_thread)

    # Signal Handling for graceful shutdown
    stop_event = threading.Event()

    def shutdown(signum, frame):
        if not stop_event.is_set():
            stop_event.set()
            print(f"\n{C_YELLOW}[*] Shutting down combined listener...{C_RESET}")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"\n{C_BOLD}{C_GREEN}🚀 COMBINED BLUETOOTH & WI-FI REMOTE ID LISTENER ACTIVE{C_RESET}")
    if is_hub_mode and forwarder:
        print(f"  • Central Hub URL    : {C_CYAN}{args.hub_url}{C_RESET} (Node ID: {C_BOLD}{args.node_id}{C_RESET})")
        if pcap_streamer and (pcap_streamer.enable_wifi or pcap_streamer.enable_ble):
            active_channels = []
            if pcap_streamer.enable_wifi:
                active_channels.append("Wi-Fi")
            if pcap_streamer.enable_ble:
                active_channels.append("BLE")
            print(
                f"  • Binary PCAP Stream : {C_MAGENTA}Concurrent {' + '.join(active_channels)} full capture streaming to Hub{C_RESET}"
            )
        print(
            f"  • Reliability Buffer : {C_MAGENTA}RAM Max: {args.max_ram_queue} pkts | Disk Spool: {args.spool_dir}/{C_RESET} "
            f"(0 disk writes on happy path)"
        )
    else:
        if db_path_to_use:
            print(
                f"  • SQLite Encounters DB: {C_MAGENTA}{db_path_to_use}{C_RESET} "
                f"(Timeout: {args.encounter_timeout_s:.0f}s / {args.encounter_timeout_s/60:.1f}m)"
            )
        if log_jsonl_to_use:
            print(f"  • Replay JSONL Log   : {C_MAGENTA}{log_jsonl_to_use}{C_RESET}")
    if log_pcap_to_use and logger_worker.pcap_logger:
        print(
            f"  • Daily PCAP Logs    : {C_MAGENTA}{logger_worker.pcap_logger.base_dir}/{C_RESET} "
            f"(Wi-Fi & BLE daily per node: {logger_worker.pcap_logger.node_id})"
        )
    print(f"{C_GRAY}Press Ctrl+C at any time to stop and view capture statistics.{C_RESET}\n")

    # Start capture threads
    for t in threads:
        t.start()

    # Main logging loop
    try:
        while not stop_event.is_set():
            now = time.time()
            try:
                event = event_queue.get(timeout=0.2)
                logger_worker.process_event(event)
            except queue.Empty:
                pass
            logger_worker.periodic_maintenance(now)
    except KeyboardInterrupt:
        shutdown(None, None)
    finally:
        # Stop all worker threads
        if hopper_thread:
            hopper_thread.stop()
        if wifi_thread:
            wifi_thread.stop()
        if ble_thread:
            ble_thread.stop()
        if pcap_streamer:
            pcap_streamer.stop()

        for t in threads:
            t.join(timeout=1.0)

        # Restore Wi-Fi interface if we configured it
        if not args.no_wifi and not args.no_wifi_setup and args.wifi_iface:
            restore_managed_mode(args.wifi_iface)

        # Drain any remaining events in queue
        while not event_queue.empty():
            try:
                logger_worker.process_event(event_queue.get_nowait())
            except Exception:
                break

        # Finalize encounters & close database/logs
        logger_worker.close()
        logger_worker.print_summary()


if __name__ == "__main__":
    main()
