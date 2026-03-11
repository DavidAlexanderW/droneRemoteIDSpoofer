from dataclasses import dataclass
from datetime import datetime
from typing import Tuple, List, Optional

from drone_rid_spoofer.helpers import random_location


@dataclass
class DroneState:
    """Represents the state of a single drone."""
    serial: bytes
    pilot_location: Tuple[int, int]
    lat: int
    lng: int
    mac_address: str
    direction: int = 0
    mode: str = "random"
    end_time: Optional[datetime] = None
    active: bool = True
    waypoints: Optional[List[Tuple[int, int, int]]] = None
    waypoint_index: int = 0
    next_waypoint_time: Optional[datetime] = None
    transport: Optional[str] = None  # per-drone transport override
    timestamp_offset: float = 0.0  # minutes to shift ASTM timestamp (negative = past)

    def update_location(self, step: int) -> None:
        """Update drone location randomly within step range."""
        self.lat, self.lng = random_location(self.lat, self.lng, step)

    def move(self, direction: str, step: int) -> None:
        """Move drone in specified direction and update rotation."""
        direction_map = {
            'north': (step, 0, 0),
            'south': (-step, 0, 180),
            'east': (0, step, 90),
            'west': (0, -step, 270)
        }

        if direction in direction_map:
            lat_delta, lng_delta, rotation = direction_map[direction]
            self.lat += lat_delta
            self.lng += lng_delta
            self.direction = rotation
