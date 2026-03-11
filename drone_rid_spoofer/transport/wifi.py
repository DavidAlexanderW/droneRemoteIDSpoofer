import logging
from typing import List

from scapy.all import sendp
import scapy.layers.dot11 as dot11

from drone_rid_spoofer.state import DroneState
from drone_rid_spoofer.transport.base import TransportBackend

logger = logging.getLogger(__name__)


class WifiBackend(TransportBackend):
    """Wi-Fi beacon frame transport for ASTM F3411-19 RID."""

    HEADER = b'\x0d\x5d\xf0\x19\x04'
    DEST_ADDR = 'ff:ff:ff:ff:ff:ff'
    DRONE_SSID = 'AnafiThermal-Spoofed'
    OUI = 16387004  # 0xFA0BBC

    def __init__(self, interface: str):
        self.interface = interface

    def send_messages(self, drone: DroneState, messages: List[bytes]) -> None:
        """Pack all messages into a single Wi-Fi beacon vendor-specific IE and send."""
        vendor_data = self.HEADER + b''.join(messages)

        ie_ssid = dot11.Dot11Elt(ID='SSID', len=len(self.DRONE_SSID), info=self.DRONE_SSID)
        ie_vendor = dot11.Dot11EltVendorSpecific(
            ID=221,
            len=len(vendor_data),
            oui=self.OUI,
            info=vendor_data,
        )

        packet = (
            dot11.RadioTap() /
            dot11.Dot11(
                type=0, subtype=8,
                addr1=self.DEST_ADDR,
                addr2=drone.mac_address,
                addr3=drone.mac_address,
            ) /
            dot11.Dot11Beacon() /
            ie_ssid /
            ie_vendor
        )

        sendp(packet, iface=self.interface, verbose=False, count=1)

    def close(self) -> None:
        pass
