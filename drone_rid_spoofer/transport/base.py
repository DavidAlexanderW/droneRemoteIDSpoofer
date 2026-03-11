from abc import ABC, abstractmethod
from typing import List

from drone_rid_spoofer.state import DroneState


class TransportBackend(ABC):
    """Abstract base class for RID transport backends."""

    @abstractmethod
    def send_messages(self, drone: DroneState, messages: List[bytes]) -> None:
        """Send ASTM RID messages for a drone.

        Args:
            drone: The drone state (provides MAC, serial, etc.)
            messages: List of 25-byte ASTM message payloads.
        """

    @abstractmethod
    def close(self) -> None:
        """Release transport resources."""
