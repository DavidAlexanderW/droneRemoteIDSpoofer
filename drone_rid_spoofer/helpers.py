import argparse
import random
from typing import Tuple


class ParseLocationAction(argparse.Action):
    """Parse location values during argument parsing."""

    def __call__(self, parser, namespace, values, option_string=None):
        coords = parse_location(values[0], values[1])
        setattr(namespace, self.dest, coords)


def parse_location(latitude: str, longitude: str) -> Tuple[int, int]:
    """Parse and validate location coordinates."""
    try:
        lat = float(latitude)
        lng = float(longitude)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid coordinate format: {latitude}, {longitude}")

    if not (-90 < lat < 90):
        raise argparse.ArgumentTypeError(f"Latitude must be between -90 and 90, got: {lat}")
    if not (-180 < lng < 180):
        raise argparse.ArgumentTypeError(f"Longitude must be between -180 and 180, got: {lng}")

    return int(lat * 10**7), int(lng * 10**7)


def generate_random_mac() -> str:
    """Generate a random MAC address.

    Top 2 bits of the MSB are forced to 11 so the address qualifies as a BLE
    Static Random Address (BT Core). Without this, controllers reject ~25% of
    fully-random MACs and treat others as RPA/NRPA, breaking BLE advertising.
    """
    parts = [random.randint(0, 255) for _ in range(6)]
    parts[0] |= 0xC0
    return ":".join(f"{b:02x}" for b in parts)


def get_random_serial_number() -> bytes:
    """Generate a random serial number for spoofed drones."""
    return f"Spoofed_Serial_{random.randint(1, 99999)}".encode()


def get_random_pilot_location(lat: int, lng: int) -> Tuple[int, int]:
    """Generate random pilot location near drone location."""
    return (
        lat + random.randint(-10000, 10000),
        lng + random.randint(-10000, 10000)
    )


def random_location(lat: int, lng: int, distance: int = 100000) -> Tuple[int, int]:
    """Generate random coordinates within specified distance."""
    return (
        lat + random.randint(-distance, distance),
        lng + random.randint(-distance, distance)
    )


def random_speed(min_mps: float = 0.0, max_mps: float = 25.0) -> float:
    """Random horizontal speed in m/s. Default range covers typical multirotors."""
    return random.uniform(min_mps, max_mps)


def random_vertical_speed(min_mps: float = -5.0, max_mps: float = 5.0) -> float:
    """Random vertical speed in m/s (positive = climbing)."""
    return random.uniform(min_mps, max_mps)


def random_altitude(min_m: float = 50.0, max_m: float = 400.0) -> float:
    """Random altitude in meters (MSL). Default range fits typical hobbyist flight ceilings."""
    return random.uniform(min_m, max_m)


def random_height(min_m: float = 10.0, max_m: float = 120.0) -> float:
    """Random height above takeoff in meters."""
    return random.uniform(min_m, max_m)


def drift(value: float, step: float, lo: float, hi: float) -> float:
    """Drift value by ±step, clamped to [lo, hi]."""
    return max(lo, min(hi, value + random.uniform(-step, step)))
