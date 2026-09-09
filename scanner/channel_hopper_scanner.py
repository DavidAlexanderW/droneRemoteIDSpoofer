#!/usr/bin/env python3
import argparse
import os
import sys
import threading
import time

# Ensure repo root is in sys.path
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scapy.all import sniff, Dot11Beacon, Dot11Elt
from drone_rid_spoofer.parser import ASTM_F3411_SpecParser


def packet_callback(pkt):
    """Callback for Scapy sniffing to detect and parse ASTM Remote ID Vendor IEs."""
    if pkt.haslayer(Dot11Beacon):
        elt = pkt.getlayer(Dot11Elt)
        while isinstance(elt, Dot11Elt):
            if elt.ID == 221 and elt.info.startswith(b'\xfa\x0b\xbc'):
                astm_payload = elt.info[5:]
                try:
                    parser = ASTM_F3411_SpecParser(astm_payload)
                    data = parser.parse_payload()
                    if data:
                        chan_info = ""
                        if pkt.haslayer("RadioTap"):
                            try:
                                freq = pkt.getlayer("RadioTap").ChannelFrequency
                                if freq == 2484:
                                    chan = 14
                                elif freq < 2484:
                                    chan = (freq - 2407) // 5
                                elif freq > 5000:
                                    chan = (freq - 5000) // 5
                                else:
                                    chan = freq
                                chan_info = f", Ch: {chan}"
                        rssi_val = "N/A"
                        if pkt.haslayer("RadioTap"):
                            rssi_val = getattr(pkt["RadioTap"], "dBm_AntSignal", "N/A")
                        print(f"\n[+] Remote ID Detected from {pkt.addr2} (RSSI: {rssi_val}dBm{chan_info})")
                        for entry in data:
                            print(f"    - {entry}")
                except Exception:
                    pass
            elt = elt.payload


def channel_hopper(interface, channels, hop_interval):
    """
    Background thread function that continually switches the interface
    channel.
    """
    print(f"[*] Starting channel hopper on {interface} across channels {channels}")
    while True:
        for ch in channels:
            # Try setting channel using iwconfig first
            ret = os.system(f"iwconfig {interface} channel {ch} 2>/dev/null")
            if ret != 0:
                # Fallback to iw if iwconfig fails or is missing
                os.system(f"iw dev {interface} set channel {ch} 2>/dev/null")
            
            # Wait for the specified interval before switching to the next channel
            time.sleep(hop_interval)


def main():
    parser = argparse.ArgumentParser(description="Wi-Fi Channel Hopper and Drone Remote ID Scanner")
    parser.add_argument("-i", "--interface", required=True, help="Monitor mode interface to use (e.g., wlan0mon, wlan1)")
    parser.add_argument("-c", "--channels", type=str, default="1,2,3,4,5,6,7,8,9,10,11,12,13", 
                        help="Comma-separated list of channels to hop through (default: 1-13)")
    parser.add_argument("-t", "--time", type=float, default=0.5, 
                        help="Time to spend on each channel in seconds (default: 0.5)")

    args = parser.parse_args()

    # Parse channels
    try:
        channels = [int(c.strip()) for c in args.channels.split(",")]
    except ValueError:
        print("[-] Error: Channels must be a comma-separated list of integers.")
        sys.exit(1)

    # Make sure we run as root (needed for iwconfig/iw and scapy sniffing)
    if os.geteuid() != 0:
        print("[-] Warning: You are not running as root. Sniffing and channel hopping usually require root privileges.")

    # Start hopper thread
    hopper_thread = threading.Thread(target=channel_hopper, args=(args.interface, channels, args.time))
    hopper_thread.daemon = True
    hopper_thread.start()

    # Start sniffing
    print(f"[*] Starting Scapy sniffer on {args.interface}...")
    try:
        sniff(iface=args.interface, prn=packet_callback, store=0)
    except KeyboardInterrupt:
        print("\n[*] Stopping scanner...")
        sys.exit(0)
    except Exception as e:
        print(f"[-] Error during sniffing: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
