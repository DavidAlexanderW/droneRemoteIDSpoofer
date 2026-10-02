#!/usr/bin/env python3
"""
Tactical Drone Remote ID - Scanner Station Configuration & Geodesy
Manages loading, validation, persistence on disk (JSON), and 3D slant range / bearing calculations.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CONFIG_FILENAME = "scanner_config.json"

DEFAULT_SCANNER_CONFIG = {
    "node_id": "sensor-node-01",
    "name": "Tactical Sensor Node 1",
    "latitude": 47.3769,
    "longitude": 8.5417,
    "altitude_m": 450.0,
    "range_rings_m": [500, 1000, 2500, 5000],
    "show_range_rings": True,
    "enabled": True,
    "locked": False,
    "description": "Ground-based Drone Remote ID Sniffer & Radar Station",
    "hub_ws_url": None,  # e.g., "ws://central-hub.local:8000/stream/node"
    "spool_dir": "spool",
    "max_ram_queue": 10000,
    "wifi_iface": None,
    "nrf_port": None,
    "enable_wifi": True,
    "enable_nrf": True,
    "coded": True,
    "ble_mode": "hop",
    "ble_bt5_dwell_s": 5.0,
    "ble_bt4_dwell_s": 1.0,
    "social_dwell_ms": 1000,
    "non_social_dwell_ms": 200,
    "non_social_dwell_5g_ms": 250,
    "non_social_ratio": 1,
    "drain_retention_ms": 5.0,
    "db_file": "rid_detections.db",
    "log_jsonl": None,
    "log_pcap": None,
    "pcap_dir": None,
    "updated_at_iso": None,
}


def get_default_config_path() -> str:
    """Returns absolute path to scanner config file from env or default location."""
    env_path = os.environ.get("RID_SCANNER_CONFIG_PATH")
    if env_path:
        return os.path.abspath(env_path)
    
    scanner_dir = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(scanner_dir, ".."))

    # 1. Check scanner/scanner_config.json
    scanner_cfg = os.path.join(scanner_dir, DEFAULT_CONFIG_FILENAME)
    if os.path.exists(scanner_cfg):
        return scanner_cfg

    # 2. Check cwd
    cwd_path = os.path.abspath(DEFAULT_CONFIG_FILENAME)
    if os.path.exists(cwd_path):
        return cwd_path

    # 3. Check repo root
    root_path = os.path.join(repo_root, DEFAULT_CONFIG_FILENAME)
    if os.path.exists(root_path):
        return root_path

    # Default to scanner/scanner_config.json
    return scanner_cfg


def load_scanner_config(config_path: Optional[str] = None, create_if_missing: bool = False) -> Dict[str, Any]:
    """
    Loads scanner configuration from JSON file on disk.
    If create_if_missing is True and the file does not exist, writes the default configuration to disk.
    Otherwise, returns an in-memory copy of DEFAULT_SCANNER_CONFIG without disk writes.
    If the file exists but contains invalid JSON, raises ValueError and NEVER overwrites it.
    """
    path = os.path.abspath(config_path) if config_path else get_default_config_path()
    
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse scanner configuration file '{path}': {e}") from e

        config = dict(DEFAULT_SCANNER_CONFIG)
        config.update(data)
        
        # Normalize types
        config["node_id"] = str(config.get("node_id") or DEFAULT_SCANNER_CONFIG["node_id"]).strip()
        config["latitude"] = float(config.get("latitude", DEFAULT_SCANNER_CONFIG["latitude"]))
        config["longitude"] = float(config.get("longitude", DEFAULT_SCANNER_CONFIG["longitude"]))
        config["altitude_m"] = float(config.get("altitude_m", DEFAULT_SCANNER_CONFIG["altitude_m"]))
        config["name"] = str(config.get("name", DEFAULT_SCANNER_CONFIG["name"]))
        config["show_range_rings"] = bool(config.get("show_range_rings", True))
        config["enabled"] = bool(config.get("enabled", True))
        config["locked"] = bool(config.get("locked", False))
        hub_url = config.get("hub_ws_url") or config.get("hub_url")
        config["hub_ws_url"] = str(hub_url).strip() if hub_url else None
        config["spool_dir"] = str(config.get("spool_dir", DEFAULT_SCANNER_CONFIG["spool_dir"]))
        config["max_ram_queue"] = int(config.get("max_ram_queue", DEFAULT_SCANNER_CONFIG["max_ram_queue"]))
        config["wifi_iface"] = config.get("wifi_iface") or config.get("wifi_interface")
        config["nrf_port"] = config.get("nrf_port")
        config["enable_wifi"] = bool(config.get("enable_wifi", True))
        config["enable_nrf"] = bool(config.get("enable_nrf", config.get("enable_ble", True)))
        config["coded"] = bool(config.get("coded", DEFAULT_SCANNER_CONFIG["coded"]))
        config["ble_mode"] = str(config.get("ble_mode", DEFAULT_SCANNER_CONFIG["ble_mode"])).lower()
        config["ble_bt5_dwell_s"] = float(config.get("ble_bt5_dwell_s", DEFAULT_SCANNER_CONFIG["ble_bt5_dwell_s"]))
        config["ble_bt4_dwell_s"] = float(config.get("ble_bt4_dwell_s", DEFAULT_SCANNER_CONFIG["ble_bt4_dwell_s"]))
        config["social_dwell_ms"] = int(config.get("social_dwell_ms", DEFAULT_SCANNER_CONFIG["social_dwell_ms"]))
        config["non_social_dwell_ms"] = int(config.get("non_social_dwell_ms", DEFAULT_SCANNER_CONFIG["non_social_dwell_ms"]))
        config["non_social_dwell_5g_ms"] = int(config.get("non_social_dwell_5g_ms", DEFAULT_SCANNER_CONFIG["non_social_dwell_5g_ms"]))
        config["non_social_ratio"] = int(config.get("non_social_ratio", DEFAULT_SCANNER_CONFIG["non_social_ratio"]))
        config["drain_retention_ms"] = float(config.get("drain_retention_ms", DEFAULT_SCANNER_CONFIG["drain_retention_ms"]))
        config["db_file"] = config.get("db_file", DEFAULT_SCANNER_CONFIG["db_file"])
        config["log_jsonl"] = config.get("log_jsonl")
        config["log_pcap"] = config.get("log_pcap")
        config["pcap_dir"] = config.get("pcap_dir") or config.get("log_pcap_dir")
        if not isinstance(config.get("range_rings_m"), list):
            config["range_rings_m"] = DEFAULT_SCANNER_CONFIG["range_rings_m"]
        
        return config

    # If file does not exist
    config = dict(DEFAULT_SCANNER_CONFIG)
    if create_if_missing:
        config["updated_at_iso"] = datetime.now(timezone.utc).isoformat()
        save_scanner_config(config, path)
    return config


def get_cli_defaults_from_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads configuration from disk without side-effects and normalizes keys
    into argparse destination attribute names for use with parser.set_defaults(**defaults).
    """
    path = os.path.abspath(config_path) if config_path else get_default_config_path()
    if not os.path.exists(path):
        return {}

    cfg = load_scanner_config(path, create_if_missing=False)
    defaults: Dict[str, Any] = {}

    # Hardware & Subsystem Toggles
    if cfg.get("enable_nrf") is False or cfg.get("enable_ble") is False:
        defaults["no_ble"] = True
    if cfg.get("enable_wifi") is False:
        defaults["no_wifi"] = True
    if "coded" in cfg and cfg["coded"] is not None:
        defaults["coded"] = bool(cfg["coded"])
    
    wifi_iface = cfg.get("wifi_iface") or cfg.get("wifi_interface")
    if wifi_iface:
        defaults["wifi_iface"] = str(wifi_iface).strip()
    if cfg.get("nrf_port"):
        defaults["nrf_port"] = str(cfg["nrf_port"]).strip()

    # Streaming & Identity
    hub_url = cfg.get("hub_ws_url") or cfg.get("hub_url")
    if hub_url:
        defaults["hub_url"] = str(hub_url).strip()
    if cfg.get("node_id"):
        defaults["node_id"] = str(cfg["node_id"]).strip()
    if cfg.get("spool_dir"):
        defaults["spool_dir"] = str(cfg["spool_dir"]).strip()
    if "max_ram_queue" in cfg and cfg["max_ram_queue"] is not None:
        defaults["max_ram_queue"] = int(cfg["max_ram_queue"])

    # BLE Parameters
    if cfg.get("ble_mode"):
        defaults["ble_mode"] = str(cfg["ble_mode"]).lower().strip()
    if "ble_bt5_dwell_s" in cfg and cfg["ble_bt5_dwell_s"] is not None:
        defaults["ble_bt5_dwell"] = float(cfg["ble_bt5_dwell_s"])
    elif "bt5_dwell" in cfg and cfg["bt5_dwell"] is not None:
        defaults["ble_bt5_dwell"] = float(cfg["bt5_dwell"])

    if "ble_bt4_dwell_s" in cfg and cfg["ble_bt4_dwell_s"] is not None:
        defaults["ble_bt4_dwell"] = float(cfg["ble_bt4_dwell_s"])
    elif "bt4_dwell" in cfg and cfg["bt4_dwell"] is not None:
        defaults["ble_bt4_dwell"] = float(cfg["bt4_dwell"])

    # Wi-Fi Hopping Parameters
    if "social_dwell_ms" in cfg and cfg["social_dwell_ms"] is not None:
        defaults["social_dwell_ms"] = int(cfg["social_dwell_ms"])
    if "non_social_dwell_ms" in cfg and cfg["non_social_dwell_ms"] is not None:
        defaults["non_social_dwell_ms"] = int(cfg["non_social_dwell_ms"])
    if "non_social_dwell_5g_ms" in cfg and cfg["non_social_dwell_5g_ms"] is not None:
        defaults["non_social_dwell_5g_ms"] = int(cfg["non_social_dwell_5g_ms"])
    if "non_social_ratio" in cfg and cfg["non_social_ratio"] is not None:
        defaults["non_social_ratio"] = int(cfg["non_social_ratio"])
    if "drain_retention_ms" in cfg and cfg["drain_retention_ms"] is not None:
        defaults["drain_retention_ms"] = float(cfg["drain_retention_ms"])

    # Storage & Logging
    if cfg.get("db_file"):
        defaults["db_file"] = str(cfg["db_file"]).strip()
    if cfg.get("log_jsonl"):
        defaults["log_jsonl"] = str(cfg["log_jsonl"]).strip()
    if cfg.get("log_pcap"):
        defaults["log_pcap"] = str(cfg["log_pcap"]).strip()
    pcap_dir = cfg.get("pcap_dir") or cfg.get("log_pcap_dir")
    if pcap_dir:
        defaults["pcap_dir"] = str(pcap_dir).strip()

    return defaults


def save_scanner_config(config_data: Dict[str, Any], config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Persists scanner station configuration to JSON file on disk.
    Preserves existing configured fields if the file is already present.
    """
    path = os.path.abspath(config_path) if config_path else get_default_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    
    merged = dict(DEFAULT_SCANNER_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
                if isinstance(existing, dict):
                    merged.update(existing)
        except Exception:
            pass

    merged.update(config_data)
    merged["locked"] = bool(config_data.get("locked", merged.get("locked", False)))
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
    and true bearing from scanner station to target aircraft.
    """
    ground_dist_m = calculate_haversine_distance_m(rx_lat, rx_lon, target_lat, target_lon)
    bearing_deg = calculate_bearing_deg(rx_lat, rx_lon, target_lat, target_lon)
    
    delta_alt_m = 0.0
    if rx_alt_m is not None and target_alt_m is not None:
        delta_alt_m = target_alt_m - rx_alt_m
        
    slant_range_m = math.sqrt(ground_dist_m ** 2 + delta_alt_m ** 2)

    return {
        "ground_distance_m": round(ground_dist_m, 1),
        "slant_range_m": round(slant_range_m, 1),
        "delta_altitude_m": round(delta_alt_m, 1),
        "bearing_deg": round(bearing_deg, 1),
    }
