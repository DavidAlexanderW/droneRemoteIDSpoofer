#!/usr/bin/env python3
"""
Tactical Drone Remote ID Dashboard - Server Launcher
Starts the FastAPI backend web server with uvicorn.
"""

import argparse
import os
import sys
import uvicorn


def parse_args():
    parser = argparse.ArgumentParser(
        description="Launch the Tactical ASTM F3411 Drone Remote ID Web Dashboard",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host address to bind the web server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="rid_detections.db",
        help="Path to SQLite detections database",
    )
    parser.add_argument(
        "--log-jsonl",
        type=str,
        default="rid_packets.jsonl",
        help="Path to JSONL packet replay log file",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Inactivity timeout in seconds to consider an encounter closed",
    )
    parser.add_argument(
        "--scanner-config",
        type=str,
        default=None,
        help="Path to JSON configuration file on disk for scanner station location and parameters (default: scanner/scanner_config.json)",
    )
    parser.add_argument(
        "--receiver-lat",
        type=float,
        default=None,
        help="Override receiver station latitude (e.g. 47.3769)",
    )
    parser.add_argument(
        "--receiver-lon",
        type=float,
        default=None,
        help="Override receiver station longitude (e.g. 8.5417)",
    )
    parser.add_argument(
        "--receiver-alt",
        type=float,
        default=None,
        help="Override receiver station altitude MSL in meters (e.g. 450.0)",
    )
    parser.add_argument(
        "--receiver-name",
        type=str,
        default=None,
        help="Override receiver sensor station name",
    )
    parser.add_argument(
        "--receiver-lock",
        action="store_true",
        default=None,
        help="Lock receiver position on disk against modifications",
    )
    parser.add_argument(
        "--receiver-unlock",
        action="store_true",
        default=None,
        help="Unlock receiver position on disk",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reloading for development",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Add scanner directory to sys.path
    scanner_dir = os.path.abspath(os.path.dirname(__file__))
    if scanner_dir not in sys.path:
        sys.path.insert(0, scanner_dir)

    try:
        from scanner.scanner_config import load_scanner_config, save_scanner_config, get_default_config_path
    except ImportError:
        from scanner_config import load_scanner_config, save_scanner_config, get_default_config_path

    # Load and optionally override scanner configuration from disk
    config_path = os.path.abspath(args.scanner_config) if args.scanner_config else get_default_config_path()
    rx_config = load_scanner_config(config_path)

    rx_updates = {}
    if args.receiver_lat is not None:
        rx_updates["latitude"] = args.receiver_lat
    if args.receiver_lon is not None:
        rx_updates["longitude"] = args.receiver_lon
    if args.receiver_alt is not None:
        rx_updates["altitude_m"] = args.receiver_alt
    if args.receiver_name is not None:
        rx_updates["name"] = args.receiver_name
    if args.receiver_lock:
        rx_updates["locked"] = True
    elif args.receiver_unlock:
        rx_updates["locked"] = False

    if rx_updates:
        rx_config.update(rx_updates)
        save_scanner_config(rx_config, config_path)

    # Pass configuration to FastAPI app via environment variables
    os.environ["RID_DB_PATH"] = os.path.abspath(args.db)
    os.environ["RID_JSONL_PATH"] = os.path.abspath(args.log_jsonl)
    os.environ["RID_TIMEOUT_S"] = str(args.timeout)
    os.environ["RID_SCANNER_CONFIG_PATH"] = config_path

    print("=" * 72)
    print("  TACTICAL DRONE REMOTE ID AIRSPACE MONITOR - WEB DASHBOARD")
    print("  ASTM F3411 / ASD-STAN Direct Broadcast Real-Time Radar")
    print("=" * 72)
    print(f"  Database       : {os.path.abspath(args.db)}")
    print(f"  JSONL Log      : {os.path.abspath(args.log_jsonl)}")
    print(f"  Scanner Config : {config_path}")
    print(f"  Scanner Node   : {rx_config['name']} ({rx_config['latitude']:.5f}°N, {rx_config['longitude']:.5f}°E, {rx_config['altitude_m']:.1f}m MSL)")
    print(f"  Listening      : http://{args.host}:{args.port}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  Local URL      : http://localhost:{args.port}")
        print(f"                   http://127.0.0.1:{args.port}")
    print("=" * 72)
    print("Press Ctrl+C to terminate the dashboard server.")
    print()

    # Determine uvicorn import string based on environment
    try:
        import scanner.dashboard.app  # noqa: F401
        app_target = "scanner.dashboard.app:app"
    except ImportError:
        app_target = "dashboard.app:app"

    uvicorn.run(
        app_target,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()

