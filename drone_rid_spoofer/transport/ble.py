"""BLE transport backend for ASTM F3411-22 Remote ID.

Uses raw HCI sockets to send BLE ADV_NONCONN_IND advertisements.
Requires Linux with root privileges (same as Wi-Fi monitor mode).

BLE AD structure (31 bytes — legacy advertising limit):
  Length:    1 byte  (0x1E = 30)
  AD Type:  1 byte  (0x16 = Service Data - 16-bit UUID)
  UUID:     2 bytes (0xFFFA little-endian → 0xFA 0xFF)
  App Code: 1 byte  (0x0D for ASTM F3411)
  Counter:  1 byte  (increments per advertisement)
  Payload:  25 bytes (one ASTM message type)

One message per advertisement — rotates through types with Location
sent at higher frequency (3x).
"""

import logging
import socket
import struct
import time
from typing import Dict, List, Optional

from drone_rid_spoofer.state import DroneState
from drone_rid_spoofer.transport.base import TransportBackend

logger = logging.getLogger(__name__)

# HCI command opcodes (OGF << 10 | OCF)
HCI_CMD_LE_SET_ADV_PARAMS = 0x2006
HCI_CMD_LE_SET_ADV_DATA = 0x2008
HCI_CMD_LE_SET_ADV_ENABLE = 0x200A
HCI_CMD_LE_SET_RANDOM_ADDR = 0x2005

# BLE RID constants
ASTM_UUID = b'\xFA\xFF'  # 0xFFFA little-endian
ASTM_APP_CODE = 0x0D
AD_TYPE_SERVICE_DATA_16 = 0x16

# Location message type byte starts with 0x10
LOCATION_MSG_PREFIX = 0x10


def _mac_to_bytes(mac: str) -> bytes:
    """Convert MAC address string to 6 bytes (reverse order for HCI)."""
    parts = mac.split(':')
    return bytes(int(p, 16) for p in reversed(parts))


def _build_hci_command(opcode: int, data: bytes) -> bytes:
    """Build an HCI command packet."""
    # HCI command packet: type(1) + opcode(2) + param_len(1) + params
    return struct.pack('<BHB', 0x01, opcode, len(data)) + data


class BleBackend(TransportBackend):
    """BLE advertisement transport for ASTM F3411-22 RID.

    Sends one ASTM message per BLE advertisement, rotating through
    message types. Location is sent at 3x frequency for compliance.

    Multi-drone constraint: BLE has one radio, so drones are
    time-multiplexed. With 200ms per advertisement and 1s interval,
    approximately 5 drones can be served per cycle.
    """

    def __init__(self, adapter: str = "hci0", advertising_interval_ms: int = 200):
        self.adapter = adapter
        self.adapter_id = int(adapter.replace("hci", ""))
        self.advertising_interval_ms = advertising_interval_ms
        self._sock: Optional[socket.socket] = None
        self._counters: Dict[bytes, int] = {}  # per-drone message counter
        self._open_socket()

    def _open_socket(self) -> None:
        """Open raw HCI socket and ensure the adapter is up."""
        try:
            # Bring the adapter up (equivalent to `hciconfig hci0 up`)
            import subprocess
            subprocess.run(
                ["hciconfig", self.adapter, "up"],
                check=True, capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise RuntimeError(
                f"Failed to bring up {self.adapter}. "
                f"Run 'sudo hciconfig {self.adapter} up' manually. Error: {e}"
            ) from e

        try:
            self._sock = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_RAW,
                socket.BTPROTO_HCI,
            )
            self._sock.bind((self.adapter_id,))
            logger.info(f"BLE backend: opened HCI socket on {self.adapter}")
        except (OSError, PermissionError) as e:
            raise RuntimeError(
                f"Failed to open HCI socket on {self.adapter}. "
                f"Requires root and Linux with Bluetooth adapter. Error: {e}"
            ) from e

    def _send_hci_command(self, opcode: int, data: bytes) -> None:
        """Send an HCI command via the raw socket."""
        cmd = _build_hci_command(opcode, data)
        self._sock.send(cmd)
        # Brief pause for controller to process
        time.sleep(0.01)

    def _set_advertising_enable(self, enable: bool) -> None:
        """Enable or disable BLE advertising."""
        self._send_hci_command(HCI_CMD_LE_SET_ADV_ENABLE, bytes([int(enable)]))

    def _set_random_address(self, mac: str) -> None:
        """Set the random advertising address."""
        addr_bytes = _mac_to_bytes(mac)
        self._send_hci_command(HCI_CMD_LE_SET_RANDOM_ADDR, addr_bytes)

    def _set_advertising_params(self) -> None:
        """Set advertising parameters for ADV_NONCONN_IND."""
        # Interval in units of 0.625ms
        interval = int(self.advertising_interval_ms / 0.625)
        params = struct.pack('<HH', interval, interval)  # min, max interval
        params += bytes([
            0x03,  # ADV_NONCONN_IND
            0x01,  # own address type: random
            0x00,  # peer address type
        ])
        params += b'\x00' * 6  # peer address
        params += bytes([
            0x07,  # channel map: all three channels (37, 38, 39)
            0x00,  # filter policy: allow all
        ])
        self._send_hci_command(HCI_CMD_LE_SET_ADV_PARAMS, params)

    def _set_advertising_data(self, message: bytes, counter: int) -> None:
        """Set the advertising data payload (31 bytes max).

        AD structure:
          [length=30][AD type=0x16][UUID 0xFFFA LE][app=0x0D][counter][25-byte payload]
        """
        ad_data = bytes([
            30,  # length of remaining AD data
            AD_TYPE_SERVICE_DATA_16,
        ]) + ASTM_UUID + bytes([
            ASTM_APP_CODE,
            counter & 0xFF,
        ]) + message

        # HCI LE Set Advertising Data: length(1) + data(31)
        hci_data = bytes([len(ad_data)]) + ad_data + b'\x00' * (31 - len(ad_data))
        self._send_hci_command(HCI_CMD_LE_SET_ADV_DATA, hci_data)

    def _get_weighted_messages(self, messages: List[bytes]) -> List[bytes]:
        """Return message list with Location sent at 3x frequency.

        For a typical [BasicID, Location, System, OperatorID] input,
        returns [BasicID, Location, Location, Location, System, OperatorID].
        """
        weighted = []
        for msg in messages:
            if len(msg) > 0 and msg[0] == LOCATION_MSG_PREFIX:
                weighted.extend([msg] * 3)
            else:
                weighted.append(msg)
        return weighted

    def send_messages(self, drone: DroneState, messages: List[bytes]) -> None:
        """Send ASTM messages as individual BLE advertisements.

        Each message is sent as a separate advertisement, rotating
        through types. Location messages are sent at 3x frequency.
        """
        if not self._sock:
            logger.error("BLE socket not open")
            return

        # Get or initialize counter for this drone
        drone_key = drone.serial
        counter = self._counters.get(drone_key, 0)

        # Disable advertising before reconfiguring
        self._set_advertising_enable(False)

        # Set random address to drone's MAC
        self._set_random_address(drone.mac_address)
        self._set_advertising_params()

        weighted_messages = self._get_weighted_messages(messages)

        for msg in weighted_messages:
            self._set_advertising_data(msg, counter)
            self._set_advertising_enable(True)

            # Let the advertisement transmit
            time.sleep(self.advertising_interval_ms / 1000.0)

            self._set_advertising_enable(False)
            counter = (counter + 1) & 0xFF

        self._counters[drone_key] = counter

    def close(self) -> None:
        """Disable advertising and close HCI socket."""
        if self._sock:
            try:
                self._set_advertising_enable(False)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
            logger.info("BLE backend: closed HCI socket")
