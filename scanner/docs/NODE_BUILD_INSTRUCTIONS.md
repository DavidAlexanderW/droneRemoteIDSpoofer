# How to Build an Autonomous Drone Remote ID Scanner Node

This guide provides step-by-step instructions for assembling the hardware and deploying the software for a dedicated, 24/7 autonomous Drone Remote ID (RID) sensor node.

Nodes can operate in **Standalone Local Mode** (logging locally to SQLite and replay files) or as part of a **Distributed Sensor Network** connected to a Central Ingestion Hub.

---

## 1. Hardware Requirements

| Component | Recommended Models / Specifications | Purpose |
| :--- | :--- | :--- |
| **Host Machine** | • Raspberry Pi 4 Model B (4GB / 8GB)<br>• Raspberry Pi 5<br>• Intel/AMD x86_64 Mini PC (NUC, Lenovo Tiny, etc.) | Runs Linux OS, packet parsing engine, channel hopping, and systemd services. |
| **Wi-Fi Adapter** *(Monitor Mode & Packet Injection)* | • **Alfa AWUS036ACM** *(MediaTek MT7612U - native Linux kernel driver `mt76x2u`, recommended)*<br>• **Alfa AWUS036ACH** *(Realtek RTL8812AU)*<br>• **Alfa AWUS036AXML** *(MediaTek MT7921AUN - Wi-Fi 6)* | Listens on 2.4 GHz and 5.8 GHz bands for ASTM F3411 802.11 Beacons and Wi-Fi NAN frames. |
| **Bluetooth Sniffer** *(BLE 4 Legacy & BLE 5 Extended)* | • **Nordic Semiconductor nRF52840 Dongle** (`PCA10059`)<br>• **Nordic nRF52840 Development Kit** (`PCA10056`) | Captures Bluetooth 4 Legacy and Bluetooth 5 Extended / LE Coded PHY advertisements over UART. |
| **Wi-Fi Antenna** | Dual-Band (2.4 GHz and 5.8 GHz) antenna with matching connector (typically RP-SMA) | Receives 802.11 Remote ID beacons across standard ASTM channels. |
| **Bluetooth Antenna** | 2.4 GHz antenna with matching connector (typically SMA or RP-SMA) | Receives BLE 4 and BLE 5 Remote ID advertisements. |
| **RF Cabling & Adapters** *(Optional)* | Coax extension cables and adapters matching your enclosure/antennas | Connects external antennas if mounting in an enclosure or mast. |
| **Power Supply & Storage** | Power supply suited for the host machine, MicroSD card or SSD | Powers the node and hosts OS and local database. |

---

## 2. Operating System Setup

Tested and supported operating systems:
- **Raspberry Pi OS 64-bit** (Debian 12 Bookworm, `aarch64` / `arm64`) on Raspberry Pi 4 / 5
- **Ubuntu 24.04 LTS** (`x86_64`)


### Step 2.1: Flash and Configure OS
Install the OS of your choice on the host machine and configure it to your liking. Be sure to enable ssh for remote access.

### Step 2.2: Enable Predictable Network Interface Naming (Raspberry Pi Only)

> [!IMPORTANT]
> **Prevent Interface Renaming Across Reboots**:
> On Raspberry Pi OS, external USB Wi-Fi dongles may randomly swap names (e.g. `wlan1` $\leftrightarrow$ `wlan0`) between reboots if kernel naming is non-deterministic. To guarantee persistent interface naming, enable predictable interface names:
>
> ```bash
> sudo raspi-config nonint do_net_names 0
> sudo reboot
> ```
> After rebooting, run `iw dev` or `ip link` to identify your monitor-capable Wi-Fi interface name (typically wlx...).

### Step 2.3: Set Local Wi-Fi Regulatory Domain
To ensure that all 5.8 GHz drone Remote ID channels (channels 149 through 173) are unlocked and scannable by the wireless regulatory subsystem:
```bash
sudo iw reg set CH   # e.g., CH (Switzerland), DE (Germany), US (United States)
```

---

## 3. Flashing nRF52840 BLE Sniffer Firmware (Optional)

If your node includes an **nRF52840 Dongle (`PCA10059`)** for Bluetooth Remote ID reception (BLE 4 Legacy and BLE 5 Long Range / LE Coded PHY), flash Nordic's official sniffer firmware before running the scanner. See the official Nordic guide [here](https://docs.nordicsemi.com/r/bundle/nrfutil/page/tools-and-integrations/wireshark/nrf-sniffer-for-bluetooth-le-capture-tool).

> [!NOTE]
> **Skipping Bluetooth**: If you only plan to sniff Wi-Fi beacons or do not have an nRF dongle attached, you can skip this step entirely and pass `--no-nrf` during software installation.

---

## 4. Automated Software Installation

Clone this repository onto the scanner node and run the automated installer script [`install_scanner.sh`](../install_scanner.sh):

```bash
git clone https://github.com/cyber-defence-campus/droneRemoteIDSpoofer.git
cd droneRemoteIDSpoofer
```

The [`install_scanner.sh`](../install_scanner.sh) script automatically handles:
1. **System Dependencies**: Installs required OS packages (`iw`, `rfkill`, `libpcap-dev`, `python3-venv`, `libusb-1.0-0`, `curl`, etc.).
2. **Nordic Tools & Firmware Utilities**:
   - Downloads architecture-specific Nordic `nrfutil` (`x86_64` or `aarch64`).
   - Installs the official `ble-sniffer` plugin.
   - Configures udev rules (`/etc/udev/rules.d/60-nrfutil.rules`) granting user access to Nordic USB CDC ACM devices (`/dev/ttyACM*`) without requiring root permissions.
3. **Python Environment**:
   - Creates a dedicated virtual environment in `.venv/`.
   - Installs project dependencies and CLI binaries (`drone-scanner`, `drone-dashboard`, `drone-query`).
4. **Linux Raw Capabilities**:
   - Grants `CAP_NET_RAW` and `CAP_NET_ADMIN` to the Python binary via `setcap`, allowing monitor mode packet capture without root escalation.
5. **Configuration Persistence**:
   - Merges hardware and network parameters into [`scanner/scanner_config.json`](scanner_config.json).
6. **systemd 24/7 Background Service**:
   - Creates and enables `/etc/systemd/system/drone-scanner.service` configured to start automatically on system boot.

---

### Providing Configuration Values to the Installer

You can configure your sensor node using either direct command-line arguments or via a declarative JSON file:

#### Method 1: Pure Command-Line Arguments (Full Single-Command Setup)
You can provide **all** parameters—including station identity, geographical coordinates, network streaming, and radio hardware—directly to `install_scanner.sh` without touching the JSON file:

| Argument | Description | Example |
| :--- | :--- | :--- |
| `--node-id <id>` | Unique station node identifier | `--node-id sensor-node-01` |
| `--name <name>` | Human-readable station name | `--name "Tactical Airspace Node"` |
| `--latitude <lat>` | Sensor station latitude in degrees | `--latitude 47.3769` |
| `--longitude <lon>` | Sensor station longitude in degrees | `--longitude 8.5417` |
| `--altitude <alt>` | Sensor station altitude in meters | `--altitude 450.0` |
| `--description <desc>` | Station description or operational notes | `--description "Rooftop Airspace Monitor"` |
| `--hub-url <url>` | Central Ingestion Hub WebSocket endpoint | `--hub-url ws://192.168.1.100:8000/stream/node` |
| `--wifi-iface <iface>` | Wi-Fi monitor interface name (auto-detected if omitted) | `--wifi-iface wlan1` or `--wifi-iface wlx...` |
| `--nrf-port <port>` | nRF USB serial port (auto-detected if omitted) | `--nrf-port /dev/ttyACM0` |
| `--no-nrf` | Disable Bluetooth scanning and eliminate USB polling loops | `--no-nrf` |
| `--scanner-config <path>` | Custom configuration path (default: `scanner/scanner_config.json`) | `--scanner-config /etc/drone-scanner/scanner_config.json` |

#### Method 2: Pre-populating `scanner_config.json` (Declarative File Approach)
If you prefer maintaining configuration declaratively in a file:
1. Copy the example configuration template:
   ```bash
   cp scanner/scanner_config.example.json scanner/scanner_config.json
   ```
2. Edit your station parameters in `scanner/scanner_config.json`.
3. Run `install_scanner.sh`. The installer will load the existing JSON file, preserve your settings, and merge any additional flags provided on the CLI.

---

### Deployment Examples

#### Example A: Standalone Local Node (Wi-Fi + Bluetooth)
Stores all flight detections locally on the node in an SQLite database (`rid_detections.db`) and optional daily replay capture files (`.jsonl`):

```bash
sudo ./scanner/install_scanner.sh \
    --with-scanner-service \
    --wifi-iface wlan1 \
    --nrf-port /dev/ttyACM0
```

#### Example B: Distributed Sensor Node (Central Hub)
Streams all decoded packets in real time over WebSockets to a Central Ingestion Hub (zero continuous disk writes, bounded 3-tier RAM queue, and local spool fallback):

```bash
sudo ./scanner/install_scanner.sh \
    --with-scanner-service \
    --wifi-iface wlan1 \
    --nrf-port /dev/ttyACM0 \
    --node-id sensor-node-01 \
    --name "Tactical Sensor Node" \
    --latitude 47.3769 \
    --longitude 8.5417 \
    --altitude 450.0 \
    --hub-url ws://<HUB-IP-OR-HOSTNAME>:8000/stream/node
```

#### Example C: Wi-Fi Only Deployment (No Bluetooth Hardware)
Works in either standalone local mode or central hub mode by adding `--no-nrf`:

```bash
# Local Wi-Fi only
sudo ./scanner/install_scanner.sh --with-scanner-service --wifi-iface wlan1 --no-nrf

# Central Hub Wi-Fi only
sudo ./scanner/install_scanner.sh --with-scanner-service --wifi-iface wlan1 --no-nrf \
    --node-id sensor-node-01 --hub-url ws://<HUB-IP>:8000/stream/node
```

---

## 5. Post-Deployment Adjustments & Runtime Overrides

All sensor settings are stored directly in [`scanner/scanner_config.json`](scanner_config.json) (or a custom path if specified via `--scanner-config <path>`):

```json
{
  "node_id": "sensor-node-01",
  "name": "Tactical Sensor Node",
  "latitude": 47.3769,
  "longitude": 8.5417,
  "altitude_m": 450.0,
  "wifi_iface": "wlan1",
  "nrf_port": "/dev/ttyACM0",
  "enable_wifi": true,
  "enable_nrf": true,
  "coded": true,
  "hub_ws_url": "ws://192.168.1.100:8000/stream/node",
  "spool_dir": "spool",
  "max_ram_queue": 10000,
  "locked": false
}
```

### Switching Between Standalone and Hub-Connected Mode
You do **not** need to re-run the installer script to change operational modes:
- **To connect a standalone node to a Central Hub**:
  Set `"hub_ws_url": "ws://<hub_ip>:8000/stream/node"` in `scanner/scanner_config.json` and restart the service:
  ```bash
  sudo systemctl restart drone-scanner.service
  ```
  The forwarder automatically initiates the WebSocket connection, syncs any historical backlog, and transitions to real-time zero-disk streaming.
- **To switch back to standalone local logging**:
  Set `"hub_ws_url": null` (or remove the key) in `scanner/scanner_config.json` and restart the service. Detections will be logged locally to `rid_detections.db`.

### Runtime CLI Overrides
Because the scanner uses hierarchical argument precedence, any value in `scanner_config.json` can be temporarily overridden when running commands manually:
```bash
# Temporarily override Wi-Fi interface and disable Bluetooth for a diagnostic run
sudo .venv/bin/drone-scanner --wifi-iface wlan0 --no-nrf
```

---

## 6. Service Management & Verification

### Managing the Background Service
```bash
# Check service health and live status
sudo systemctl status drone-scanner.service

# View live colorized scanner logs in real-time
sudo journalctl -u drone-scanner.service -f

# Restart or stop the scanner service
sudo systemctl restart drone-scanner.service
sudo systemctl stop drone-scanner.service
```

### Verifying Hardware Subsystems

1. **Verify Wi-Fi Monitor Mode**:
   ```bash
   iw dev
   # Ensure your Wi-Fi interface (e.g. wlan1 or wlx...) is present and supports monitor mode:
   iw list | grep -A 10 "Supported interface modes"
   ```

2. **Verify Nordic nRF Dongle**:
   ```bash
   # Check if USB serial port exists
   ls -l /dev/ttyACM*
   
   # Verify nrfutil and ble-sniffer plugin execution
   nrfutil ble-sniffer --help
   ```

3. **Verify Database Growth & Encounters**:
   ```bash
   # Inspect logged encounters
   .venv/bin/drone-query stats
   ```

---

## 7. Next Steps: Multi-Node Deployment & Central Hub

If you are deploying multiple sensor nodes across an airspace or want to aggregate detections onto a centralized tactical radar dashboard:
- **Central Ingestion Hub & Tactical Dashboard**: To deploy the aggregation server (`drone-central-hub`) and real-time radar web UI (`drone-dashboard`), see [CENTRAL_HUB_AND_VPN_DEPLOYMENT.md](CENTRAL_HUB_AND_VPN_DEPLOYMENT.md).
- **Secure Network Interconnect**: For recommendations on securing node-to-hub communications (e.g. via VPNs such as WireGuard or Tailscale, private APNs, or encrypted tunnels), see [CENTRAL_HUB_AND_VPN_DEPLOYMENT.md](CENTRAL_HUB_AND_VPN_DEPLOYMENT.md#5-secure-network-interconnect).