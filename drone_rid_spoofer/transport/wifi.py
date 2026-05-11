import logging
from typing import List

from scapy.all import sendp
import scapy.layers.dot11 as dot11

from drone_rid_spoofer.state import DroneState
from drone_rid_spoofer.messages import MsgType
from drone_rid_spoofer.transport.base import TransportBackend

logger = logging.getLogger(__name__)


class WifiBackend(TransportBackend):
    """Wi-Fi beacon frame transport for ASTM F3411-19 RID."""

    APP_CODE = 0x0D
    DEST_ADDR = 'ff:ff:ff:ff:ff:ff'
    SSID_PREFIX = 'RID-'
    SSID_MAX_LEN = 32
    OUI = b'\xfa\x0b\xbc'
    SUPPORTED_RATES = b'\x82\x84\x8b\x96\x24\x30\x48\x6c'

    def __init__(self, interface: str, ess: bool = False, protocol_version: int = 2):
        self.interface = interface
        self.ess = ess
        self.protocol_version = protocol_version
        
        # Message pack header: msg_type (0xF = Pack) + ver, msg_size (0x19 = 25 bytes)
        self.pack_header_prefix = bytes([(MsgType.PACK << 4) | self.protocol_version, 0x19])
        
        # ODID message-pack counter: must increment per transmission so
        # receivers treat each beacon as a fresh message rather than a duplicate.
        self._counter = 0

    def send_messages(self, drone: DroneState, messages: List[bytes]) -> None:
        """Pack all messages into a single Wi-Fi beacon vendor-specific IE and send."""
        msg_count = bytes([len(messages) & 0xFF])
        header = bytes([self.APP_CODE, self._counter]) + self.pack_header_prefix + msg_count
        self._counter = (self._counter + 1) & 0xFF
        vendor_data = header + b''.join(messages)

        serial_str = drone.serial.decode('ascii', errors='replace')
        ssid = (self.SSID_PREFIX + serial_str)[: self.SSID_MAX_LEN]
        ie_ssid = dot11.Dot11Elt(ID='SSID', info=ssid)
        ie_rates = dot11.Dot11Elt(ID='Rates', info=self.SUPPORTED_RATES)
        # Build the vendor-specific IE as raw bytes so the element length
        # covers both the OUI and the Remote ID payload.
        ie_vendor = dot11.Dot11Elt(ID=221, info=self.OUI + vendor_data)

        packet = (
            dot11.RadioTap() /
            dot11.Dot11(
                type=0, subtype=8,
                addr1=self.DEST_ADDR,
                addr2=drone.mac_address,
                addr3=drone.mac_address,
            ) /
            dot11.Dot11Beacon(cap='ESS' if self.ess else 0) /
            ie_ssid /
            ie_rates /
            ie_vendor
        )

        sendp(packet, iface=self.interface, verbose=False, count=1)

    def close(self) -> None:
        pass
