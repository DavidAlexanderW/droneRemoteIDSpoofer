#!/usr/bin/env python3
"""
Tactical Drone Remote ID - Receiver Station Configuration & Geodesy
Manages loading, validation, persistence on disk (JSON), and 3D slant range / bearing calculations.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CONFIG_FILENAME = "receiver_config.json"

DEFAULT_RECEIVER_CONFIG = {
    "name": "Tactical Sensor Node 1",
    "latitude": 47.3769,
    "longitude": 8.5417,
    "altitude_m": 450.0,
    "range_rings_m": [500, 1000, 2500, 5000],
    "show_range_rings": True,
    "enabled": True,
    "locked": False,
    "description": "Ground-based Drone Remote ID Sniffer & Radar Station",
    "updated_at_iso": None,
}


def get_default_config_path() -> str:
    """Returns absolute path to receiver config file from env or default location."""
    env_path = os.environ.get("RID_RECEIVER_CONFIG_PATH")
    if env_path:
        return os.path.abspath(env_path)
    
    # Check if receiver_config.json exists in cwd or repo root
    cwd_path = os.path.abspath(DEFAULT_CONFIG_FILENAME)
    if os.path.exists(cwd_path):
        return cwd_path
    
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root_path = os.path.join(repo_root, DEFAULT_CONFIG_FILENAME)
    if os.path.exists(root_path):
        return root_path
        
    return cwd_path


def load_receiver_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads receiver configuration from JSON file on disk.
    If the file does not exist, writes the default configuration to disk.
    """
    path = os.path.abspath(config_path) if config_path else get_default_config_path()
    
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = dict(DEFAULT_RECEIVER_CONFIG)
                config.update(data)
                
                # Normalize types
                config["latitude"] = float(config.get("latitude", DEFAULT_RECEIVER_CONFIG["latitude"]))
                config["longitude"] = float(config.get("longitude", DEFAULT_RECEIVER_CONFIG["longitude"]))
                config["altitude_m"] = float(config.get("altitude_m", DEFAULT_RECEIVER_CONFIG["altitude_m"]))
                config["name"] = str(config.get("name", DEFAULT_RECEIVER_CONFIG["name"]))
                config["show_range_rings"] = bool(config.get("show_range_rings", True))
                config["enabled"] = bool(config.get("enabled", True))
                config["locked"] = bool(config.get("locked", False))
                if not isinstance(config.get("range_rings_m"), list):
                    config["range_rings_m"] = DEFAULT_RECEIVER_CONFIG["range_rings_m"]
                
                return config
        except Exception as e:
            # If reading failed, fall back to default
            pass

    # Create default config on disk
    config = dict(DEFAULT_RECEIVER_CONFIG)
    config["updated_at_iso"] = datetime.now(timezone.utc).isoformat()
    save_receiver_config(config, path)
    return config


def save_receiver_config(config_data: Dict[str, Any], config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Persists receiver station configuration to JSON file on disk.
    """
    path = os.path.abspath(config_path) if config_path else get_default_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    
    merged = dict(DEFAULT_RECEIVER_CONFIG)
    merged.update(config_data)
    merged["locked"] = bool(config_data.get("locked", False))
    merged["updated_at_iso"] = datetime.now(timezone.utc).isoformat()
    
    # Atomic write to prevent partial file writes
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp_path, path)
    
    return merged


def calculate_haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates ground distance in meters between two lat/lon coordinates."""
    r = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def calculate_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates true initial bearing (azimuth 0-360 deg) from point 1 to point 2."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    theta = math.atan2(y, x)
    return (math.degrees(theta) + 360.0) % 360.0


def calculate_slant_range_and_bearing(
    rx_lat: float,
    rx_lon: float,
    rx_alt_m: Optional[float],
    target_lat: float,
    target_lon: float,
    target_alt_m: Optional[float],
) -> Dict[str, Any]:
    """
    Computes 2D ground distance, 3D slant range (accounting for altitude delta),
    and true bearing from receiver to target aircraft.
    """
    ground_dist_m = calculate_haversine_distance_m(rx_lat, rx_lon, target_lat, target_lon)
    bearing_deg = calculate_bearing_deg(rx_lat, rx_lon, target_lat, target_lon)
    
    delta_alt_m = 0.0
    if rx_alt_m is not None and target_alt_m is not None:
        delta_alt_m = target_alt_m - rx_alt_m
        slant_range_m = math.sqrt(ground_dist_m ** 2 + delta_alt_m ** 2)
    else:
        slant_range_m = ground_dist_m

    return {
        "ground_distance_m": round(ground_dist_m, 1),
        "slant_range_m": round(slant_range_m, 1),
        "delta_alt_m": round(delta_alt_m, 1),
        "bearing_deg": round(bearing_deg, 1),
    }
