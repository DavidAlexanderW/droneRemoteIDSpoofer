#!/usr/bin/env python3
"""
Tactical Drone Remote ID - Central Dashboard & Viewport Configuration
Manages loading, validation, and disk persistence (JSON) for the Tactical Web Dashboard.
Separates central dashboard presentation parameters from edge scanner station hardware configs.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DASHBOARD_CONFIG_FILENAME = "dashboard_config.json"

DEFAULT_DASHBOARD_CONFIG: Dict[str, Any] = {
    "title": "Tactical Drone Remote ID Radar & Airspace Monitor",
    "center_latitude": 47.3769,
    "center_longitude": 8.5417,
    "default_zoom": 13,
    "show_range_rings": True,
    "show_trails": True,
    "show_waypoints": True,
    "description": "Central Tactical Airspace Radar & Drone Remote ID Operations Viewport",
    "updated_at_iso": None,
}


def get_default_dashboard_config_path() -> str:
    """Returns absolute path to dashboard config file from env or standard default locations."""
    env_path = os.environ.get("RID_DASHBOARD_CONFIG_PATH")
    if env_path:
        return os.path.abspath(env_path)

    dashboard_dir = os.path.abspath(os.path.dirname(__file__))
    scanner_dir = os.path.abspath(os.path.join(dashboard_dir, ".."))
    repo_root = os.path.abspath(os.path.join(scanner_dir, ".."))

    # 1. Check scanner/dashboard/dashboard_config.json
    dash_cfg = os.path.join(dashboard_dir, DEFAULT_DASHBOARD_CONFIG_FILENAME)
    if os.path.exists(dash_cfg):
        return dash_cfg

    # 2. Check scanner/dashboard_config.json
    scanner_dash_cfg = os.path.join(scanner_dir, DEFAULT_DASHBOARD_CONFIG_FILENAME)
    if os.path.exists(scanner_dash_cfg):
        return scanner_dash_cfg

    # 3. Check cwd
    cwd_path = os.path.abspath(DEFAULT_DASHBOARD_CONFIG_FILENAME)
    if os.path.exists(cwd_path):
        return cwd_path

    # 4. Check repo root
    root_path = os.path.join(repo_root, DEFAULT_DASHBOARD_CONFIG_FILENAME)
    if os.path.exists(root_path):
        return root_path

    # Default to scanner/dashboard/dashboard_config.json
    return dash_cfg


def load_dashboard_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads dashboard configuration from JSON file on disk.
    If the file does not exist, writes the default configuration to disk.
    If the file exists but contains invalid JSON, raises ValueError and NEVER overwrites it.
    """
    path = os.path.abspath(config_path) if config_path else get_default_dashboard_config_path()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse dashboard configuration file '{path}': {e}") from e

        config = dict(DEFAULT_DASHBOARD_CONFIG)
        config.update(data)

        # Normalize latitude/longitude keys
        lat = data.get("center_latitude", data.get("latitude", DEFAULT_DASHBOARD_CONFIG["center_latitude"]))
        lon = data.get("center_longitude", data.get("longitude", DEFAULT_DASHBOARD_CONFIG["center_longitude"]))

        config["center_latitude"] = float(lat)
        config["center_longitude"] = float(lon)
        config["default_zoom"] = int(data.get("default_zoom", DEFAULT_DASHBOARD_CONFIG["default_zoom"]))
        config["title"] = str(data.get("title", DEFAULT_DASHBOARD_CONFIG["title"]))
        config["show_range_rings"] = bool(data.get("show_range_rings", True))
        config["show_trails"] = bool(data.get("show_trails", True))
        config["show_waypoints"] = bool(data.get("show_waypoints", True))

        return config

    # Create default config on disk only if file does not exist
    config = dict(DEFAULT_DASHBOARD_CONFIG)
    config["updated_at_iso"] = datetime.now(timezone.utc).isoformat()
    save_dashboard_config(config, path)
    return config


def save_dashboard_config(config_data: Dict[str, Any], config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Persists dashboard configuration to JSON file on disk.
    """
    path = os.path.abspath(config_path) if config_path else get_default_dashboard_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    merged = dict(DEFAULT_DASHBOARD_CONFIG)
    merged.update(config_data)

    if "center_latitude" in config_data:
        merged["center_latitude"] = float(config_data["center_latitude"])
    elif "latitude" in config_data:
        merged["center_latitude"] = float(config_data["latitude"])

    if "center_longitude" in config_data:
        merged["center_longitude"] = float(config_data["center_longitude"])
    elif "longitude" in config_data:
        merged["center_longitude"] = float(config_data["longitude"])

    merged["updated_at_iso"] = datetime.now(timezone.utc).isoformat()

    # Atomic write
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp_path, path)

    return merged
