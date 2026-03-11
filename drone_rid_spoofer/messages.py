import struct
from datetime import datetime, timedelta
from typing import Tuple, List

from drone_rid_spoofer.state import DroneState


def _transform_rotation(rotation: int) -> Tuple[int, int]:
    """Transform rotation value according to ASTM F3411-19."""
    rotation = max(0, min(359, rotation))
    if rotation < 180:
        return rotation, 32
    else:
        return rotation - 180, 34


def build_basic_id(serial: bytes) -> bytes:
    """Build Message Type 0 - Basic ID (25 bytes)."""
    serial_packed = struct.pack("<20s", serial)
    return b'\x00\x12' + serial_packed + b'\x00\x00\x00'


def build_location_vector(lat: int, lng: int, direction: int,
                          timestamp_offset: float = 0.0) -> bytes:
    """Build Message Type 1 - Location/Vector (25 bytes).

    Args:
        timestamp_offset: Minutes to shift the ASTM timestamp.
            Negative values produce timestamps in the past.
            The field wraps within the current hour (0-5999 tenth-seconds).
    """
    dir_val, ew_dir = _transform_rotation(direction)
    now = datetime.now() + timedelta(minutes=timestamp_offset)
    tenth_seconds = (now.minute * 600 + now.second * 10) % 6000

    return b''.join([
        struct.pack("<B", 0x10),
        struct.pack("<B", ew_dir),
        struct.pack("<B", dir_val),
        b'\x00\x00',
        struct.pack("<i", lat),
        struct.pack("<i", lng),
        b'\x00\x00\x00\x00',
        struct.pack("<H", 0x07d0),
        b'\x00\x00',
        struct.pack("<H", tenth_seconds),
        b'\x00\x00'
    ])


def build_system(pilot_lat: int, pilot_lng: int) -> bytes:
    """Build Message Type 4 - System (25 bytes)."""
    return b''.join([
        struct.pack("<B", 0x40),
        struct.pack("<B", 0x05),
        struct.pack("<i", pilot_lat),
        struct.pack("<i", pilot_lng),
        b'\x00\x00\x00\x00\x00\x00\x00',
        struct.pack("<B", 0x12),
        b'\x00\x00\x00\x00\x00\x00\x00'
    ])


def build_operator_id() -> bytes:
    """Build Message Type 5 - Operator ID (25 bytes)."""
    return b'\x50' + b'\x00' * 24


def build_all_messages(drone: DroneState) -> List[bytes]:
    """Build all ASTM message payloads for a drone."""
    return [
        build_basic_id(drone.serial),
        build_location_vector(drone.lat, drone.lng, drone.direction,
                              drone.timestamp_offset),
        build_system(drone.pilot_location[0], drone.pilot_location[1]),
        build_operator_id(),
    ]
