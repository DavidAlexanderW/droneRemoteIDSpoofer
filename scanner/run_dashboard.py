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
        default=None,
        help="Path to SQLite detections database (default: auto-detects rid_detections_central.db if present, else rid_detections.db)",
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
        "--dashboard-config",
        "--config",
        type=str,
        default=None,
        help="Path to JSON configuration file on disk for dashboard viewport parameters (default: scanner/dashboard/dashboard_config.json)",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="Override dashboard radar title",
    )
    parser.add_argument(
        "--center-lat",
        type=float,
        default=None,
        help="Override default map center latitude (e.g. 47.3769)",
    )
    parser.add_argument(
        "--center-lon",
        type=float,
        default=None,
        help="Override default map center longitude (e.g. 8.5417)",
    )
    parser.add_argument(
        "--zoom",
        type=int,
        default=None,
        help="Override default map zoom level (e.g. 13)",
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
        from scanner.dashboard.dashboard_config import (
            load_dashboard_config,
            save_dashboard_config,
            get_default_dashboard_config_path,
        )
    except ImportError:
        try:
            from dashboard.dashboard_config import (
                load_dashboard_config,
                save_dashboard_config,
                get_default_dashboard_config_path,
            )
        except ImportError:
            from dashboard_config import (
                load_dashboard_config,
                save_dashboard_config,
                get_default_dashboard_config_path,
            )

    # Load and optionally override dashboard configuration from disk
    chosen_cfg_path = args.dashboard_config
    config_path = os.path.abspath(chosen_cfg_path) if chosen_cfg_path else get_default_dashboard_config_path()
    dash_config = load_dashboard_config(config_path)

    updates = {}
    if args.title is not None:
        updates["title"] = args.title
    if args.center_lat is not None:
        updates["center_latitude"] = args.center_lat
    if args.center_lon is not None:
        updates["center_longitude"] = args.center_lon
    if args.zoom is not None:
        updates["default_zoom"] = args.zoom

    if updates:
        dash_config.update(updates)
        save_dashboard_config(dash_config, config_path)

    # Resolve database path (explicit flag > central DB on disk > standard DB)
    repo_root = os.path.abspath(os.path.join(scanner_dir, ".."))
    if args.db is not None:
        db_path = os.path.abspath(args.db)
    else:
        candidates = [
            "rid_detections_central.db",
            os.path.join(repo_root, "rid_detections_central.db"),
            "rid_detections.db",
            os.path.join(repo_root, "rid_detections.db"),
        ]
        db_path = None
        for cand in candidates:
            if os.path.isfile(cand):
                db_path = os.path.abspath(cand)
                break
        if db_path is None:
            db_path = os.path.abspath("rid_detections.db")

    # Pass configuration to FastAPI app via environment variables
    os.environ["RID_DB_PATH"] = db_path
    os.environ["RID_JSONL_PATH"] = os.path.abspath(args.log_jsonl)
    os.environ["RID_TIMEOUT_S"] = str(args.timeout)
    os.environ["RID_DASHBOARD_CONFIG_PATH"] = config_path

    title = dash_config.get("title", "Tactical Drone Remote ID Radar")
    c_lat = dash_config.get("center_latitude", 47.3769)
    c_lon = dash_config.get("center_longitude", 8.5417)
    zoom = dash_config.get("default_zoom", 13)

    print("=" * 72)
    print(f"  {title.upper()}")
    print("  ASTM F3411 / ASD-STAN Direct Broadcast Real-Time Radar")
    print("=" * 72)
    print(f"  Database         : {db_path}")
    print(f"  JSONL Log        : {os.path.abspath(args.log_jsonl)}")
    print(f"  Dashboard Config : {config_path}")
    print(f"  Default Viewport : {c_lat:.5f}°N, {c_lon:.5f}°E (Zoom: {zoom})")
    print(f"  Listening        : http://{args.host}:{args.port}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  Local URL        : http://localhost:{args.port}")
        print(f"                     http://127.0.0.1:{args.port}")
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

