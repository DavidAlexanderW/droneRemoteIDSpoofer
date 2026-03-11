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
    """Generate a random MAC address."""
    return ":".join([f"{random.randint(0, 255):02x}" for _ in range(6)])


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
