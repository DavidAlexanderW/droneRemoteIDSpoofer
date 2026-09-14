#!/usr/bin/env bash
# ==============================================================================
# Drone Remote ID Scanner - Automated Cross-Platform Node Installer
# Supports: Ubuntu, Debian, Raspberry Pi OS (x86_64, amd64, aarch64, arm64)
# ==============================================================================
#
# This script automates:
#   1. System packages installation (iw, iproute2, rfkill, wireless-tools, curl, etc.)
#   2. Architecture detection (x86_64 / amd64 vs aarch64 / arm64)
#   3. Nordic nrfutil binary installation, ble-sniffer plugin setup & /usr/local/bin symlinking
#   4. udev rules for Nordic USB Dongles & user permissions (dialout group)
#   5. Python virtual environment creation & editable package installation (pip install -e .[all])
#   6. Linux raw socket capabilities configuration (setcap)
#   7. Optional systemd service units installation (drone-scanner & drone-dashboard)
#
# Usage:
#   ./scanner/install_scanner.sh [OPTIONS]
#
# Options:
#   --with-services       Automatically configure and enable systemd background services
#   --wifi-iface <iface>  Default Wi-Fi monitor interface for systemd service (e.g. wlan1)
#   --nrf-port <port>     Default nRF serial port for systemd service (e.g. /dev/ttyACM0)
#   --no-nrf              Skip downloading nrfutil and ble-sniffer plugin
#   --no-sys-pkgs         Skip apt-get system packages installation
#   --no-caps             Skip granting Linux network capabilities (setcap)
#   -h, --help            Show this help message
# ==============================================================================

set -eo pipefail

# ANSI Colors
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_GREEN="\033[92m"
C_BLUE="\033[94m"
C_CYAN="\033[96m"
C_YELLOW="\033[93m"
C_RED="\033[91m"

# Script & Repository Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Identify invoking user (handles running under sudo)
if [ -n "${SUDO_USER}" ] && [ "${SUDO_USER}" != "root" ]; then
    REAL_USER="${SUDO_USER}"
    USER_HOME=$(getent passwd "${SUDO_USER}" | cut -d: -f6)
else
    REAL_USER="${USER:-$(whoami)}"
    USER_HOME="${HOME}"
fi

# Default options
INSTALL_SERVICES=false
WIFI_IFACE=""
NRF_PORT=""
HUB_URL=""
NODE_ID=""
SKIP_NRF=false
SKIP_SYS_PKGS=false
SKIP_CAPS=false

# Helper for running commands with sudo if not already root
run_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

print_header() {
    echo -e "${C_CYAN}${C_BOLD}"
    echo "=============================================================================="
    echo "  DRONE REMOTE ID SCANNER & RADAR - NODE INSTALLER"
    echo "  ASTM F3411 / OpenDroneID Combined Multi-Band Airspace Monitor"
    echo "=============================================================================="
    echo -e "${C_RESET}"
}

print_usage() {
    print_header
    echo -e "${C_BOLD}Usage:${C_RESET} $0 [OPTIONS]"
    echo ""
    echo -e "${C_BOLD}Options:${C_RESET}"
    echo "  --with-services          Configure and install systemd 24/7 background scanner service"
    echo "  --with-dashboard-service Configure and install systemd 24/7 Tactical Radar Dashboard service"
    echo "  --with-hub-service       Configure and install systemd 24/7 Central Ingestion Hub service"
    echo "  --wifi-iface <iface>     Wi-Fi interface to set in drone-scanner.service (e.g. wlan0, wlan1)"
    echo "  --nrf-port <port>        nRF UART serial port to set in drone-scanner.service (e.g. /dev/ttyACM0)"
    echo "  --scanner-config <path>   Path to scanner_config.json (default: <repo>/scanner/scanner_config.json)"
    echo "  --dashboard-config <path> Path to dashboard_config.json (default: <repo>/scanner/dashboard/dashboard_config.json)"
    echo "  --hub-url <url>           Central Ingestion Hub WebSocket URL (e.g. ws://hub-ip:8000/stream/node)"
    echo "  --node-id <id>            Unique sensor node identifier (e.g. sensor-node-01)"
    echo "  --no-nrf                  Skip downloading Nordic nrfutil and ble-sniffer plugin"
    echo "  --no-sys-pkgs             Skip apt package installation (iw, iproute2, rfkill, etc.)"
    echo "  --no-caps                 Skip Linux network capabilities (setcap)"
    echo "  -h, --help                Show this help message and exit"
    echo ""
}

# Parse Arguments
INSTALL_SERVICES=false
INSTALL_DASHBOARD_SERVICE=false
INSTALL_HUB_SERVICE=false
SCANNER_CONFIG="${REPO_DIR}/scanner/scanner_config.json"
DASHBOARD_CONFIG="${REPO_DIR}/scanner/dashboard/dashboard_config.json"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-services)
            INSTALL_SERVICES=true
            shift
            ;;
        --with-dashboard-service)
            INSTALL_DASHBOARD_SERVICE=true
            shift
            ;;
        --with-hub-service)
            INSTALL_HUB_SERVICE=true
            shift
            ;;
        --wifi-iface)
            WIFI_IFACE="$2"
            shift 2
            ;;
        --nrf-port)
            NRF_PORT="$2"
            shift 2
            ;;
        --scanner-config)
            SCANNER_CONFIG="$2"
            shift 2
            ;;
        --dashboard-config)
            DASHBOARD_CONFIG="$2"
            shift 2
            ;;
        --hub-url)
            HUB_URL="$2"
            shift 2
            ;;
        --node-id)
            NODE_ID="$2"
            shift 2
            ;;
        --no-nrf)
            SKIP_NRF=true
            shift
            ;;
        --no-sys-pkgs)
            SKIP_SYS_PKGS=true
            shift
            ;;
        --no-caps)
            SKIP_CAPS=true
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo -e "${C_RED}[!] Unknown option: $1${C_RESET}"
            print_usage
            exit 1
            ;;
    esac
done

print_header

echo -e "${C_BLUE}[*] Target Host Architecture:${C_RESET} $(uname -m)"
echo -e "${C_BLUE}[*] Repository Root Directory:${C_RESET} ${REPO_DIR}"
echo -e "${C_BLUE}[*] Installing For User      :${C_RESET} ${REAL_USER}"
echo ""

# ------------------------------------------------------------------------------
# 1. System Packages Installation (Debian/Ubuntu/Raspberry Pi OS)
# ------------------------------------------------------------------------------
if [ "$SKIP_SYS_PKGS" = false ]; then
    if command -v apt-get >/dev/null 2>&1; then
        echo -e "${C_BOLD}${C_GREEN}[1/6] Checking & Installing OS System Packages (apt)...${C_RESET}"
        REQUIRED_PKGS=(iw iproute2 rfkill wireless-tools python3 python3-venv python3-pip libcap2-bin curl udev kmod build-essential)
        MISSING_PKGS=()
        
        if command -v dpkg-query >/dev/null 2>&1; then
            for pkg in "${REQUIRED_PKGS[@]}"; do
                if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed"; then
                    MISSING_PKGS+=("$pkg")
                fi
            done
        else
            MISSING_PKGS=("${REQUIRED_PKGS[@]}")
        fi

        if [ ${#MISSING_PKGS[@]} -eq 0 ]; then
            echo -e "${C_GREEN}[+] All required OS packages are already installed.${C_RESET}\n"
        else
            echo -e "${C_CYAN}[*] Installing missing package(s): ${MISSING_PKGS[*]}...${C_RESET}"
            run_sudo apt-get update -qq || true
            run_sudo apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}"
            echo -e "${C_GREEN}[+] System dependencies installed.${C_RESET}\n"
        fi
    else
        echo -e "${C_YELLOW}[!] Non-Debian/Ubuntu distribution detected. Ensure iw, iproute2, rfkill, and python3-venv are installed.${C_RESET}\n"
    fi
else
    echo -e "${C_YELLOW}[!] Skipping OS system packages installation (--no-sys-pkgs).${C_RESET}\n"
fi

# ------------------------------------------------------------------------------
# 2. Nordic Semiconductor nrfutil & ble-sniffer Plugin Setup
# ------------------------------------------------------------------------------
if [ "$SKIP_NRF" = false ]; then
    echo -e "${C_BOLD}${C_GREEN}[2/6] Configuring Nordic nrfutil & BLE Sniffer...${C_RESET}"
    
    RAW_ARCH=$(uname -m)
    NRFUTIL_URL=""
    
    case "${RAW_ARCH}" in
        x86_64|amd64)
            NRFUTIL_URL="https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil"
            ;;
        aarch64|arm64)
            NRFUTIL_URL="https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/aarch64-unknown-linux-gnu/nrfutil"
            ;;
        *)
            echo -e "${C_YELLOW}[!] Unsupported architecture (${RAW_ARCH}) for precompiled nrfutil binary.${C_RESET}"
            echo -e "${C_YELLOW}[!] You can manually install nrfutil from Nordic's website if needed.${C_RESET}"
            ;;
    esac

    if [ -n "${NRFUTIL_URL}" ]; then
        if command -v nrfutil >/dev/null 2>&1; then
            NRFUTIL_BIN=$(which nrfutil)
            echo -e "${C_GREEN}[+] nrfutil is already installed at: ${NRFUTIL_BIN}${C_RESET}"
            echo -e "    Version: $(nrfutil --version 2>&1 | head -n 1 || true)"
        else
            echo -e "${C_CYAN}[*] Downloading nrfutil for ${RAW_ARCH} from Nordic Artifactory...${C_RESET}"
            TMP_NRF="/tmp/nrfutil_installer_$$"
            curl -fsSL "${NRFUTIL_URL}" -o "${TMP_NRF}"
            chmod +x "${TMP_NRF}"
            run_sudo mv "${TMP_NRF}" /usr/local/bin/nrfutil
            run_sudo chmod +x /usr/local/bin/nrfutil
            echo -e "${C_GREEN}[+] nrfutil installed to /usr/local/bin/nrfutil.${C_RESET}"
        fi

        # Locate ble-sniffer plugin executable
        find_ble_sniffer_binary() {
            local search_paths=(
                "${USER_HOME}/.nrfutil/bin/nrfutil-ble-sniffer"
                "/root/.nrfutil/bin/nrfutil-ble-sniffer"
                "${HOME}/.nrfutil/bin/nrfutil-ble-sniffer"
            )
            for path in "${search_paths[@]}"; do
                if [ -f "${path}" ] && [ -x "${path}" ]; then
                    echo "${path}"
                    return 0
                fi
            done
            if [ -d "${USER_HOME}/.nrfutil" ]; then
                local found
                found=$(find "${USER_HOME}/.nrfutil" -type f -name "nrfutil-ble-sniffer" 2>/dev/null | head -n 1)
                if [ -n "${found}" ] && [ -x "${found}" ]; then
                    echo "${found}"
                    return 0
                fi
            fi
            if [ -d "/root/.nrfutil" ]; then
                local found_root
                found_root=$(find "/root/.nrfutil" -type f -name "nrfutil-ble-sniffer" 2>/dev/null | head -n 1)
                if [ -n "${found_root}" ] && [ -x "${found_root}" ]; then
                    echo "${found_root}"
                    return 0
                fi
            fi
            return 1
        }

        BLE_SNIFFER_BIN=$(find_ble_sniffer_binary || true)

        if [ -z "${BLE_SNIFFER_BIN}" ] || ! nrfutil ble-sniffer --help >/dev/null 2>&1; then
            echo -e "${C_CYAN}[*] Installing 'ble-sniffer' plugin via nrfutil...${C_RESET}"
            if [ "${REAL_USER}" != "root" ] && command -v sudo >/dev/null 2>&1; then
                sudo -u "${REAL_USER}" nrfutil install ble-sniffer 2>/dev/null || true
            fi
            nrfutil install ble-sniffer 2>/dev/null || run_sudo nrfutil install ble-sniffer 2>/dev/null || true
            BLE_SNIFFER_BIN=$(find_ble_sniffer_binary || true)
        fi

        # Ensure ble-sniffer executable is symlinked into PATH (/usr/local/bin/nrfutil-ble-sniffer)
        if [ -n "${BLE_SNIFFER_BIN}" ]; then
            # Ensure permissions on source binary and its parent directory
            run_sudo chmod 755 "${BLE_SNIFFER_BIN}" 2>/dev/null || true
            run_sudo chmod 755 "$(dirname "${BLE_SNIFFER_BIN}")" 2>/dev/null || true

            TARGET_SYMLINK="/usr/local/bin/nrfutil-ble-sniffer"
            if [ -L "${TARGET_SYMLINK}" ] && [ "$(readlink -f "${TARGET_SYMLINK}")" = "$(readlink -f "${BLE_SNIFFER_BIN}")" ]; then
                echo -e "${C_GREEN}[+] ble-sniffer binary already symlinked in PATH: ${TARGET_SYMLINK} -> ${BLE_SNIFFER_BIN}${C_RESET}"
            else
                echo -e "${C_CYAN}[*] Symlinking ${BLE_SNIFFER_BIN} -> ${TARGET_SYMLINK}...${C_RESET}"
                run_sudo ln -sf "${BLE_SNIFFER_BIN}" "${TARGET_SYMLINK}"
                run_sudo chmod +x "${TARGET_SYMLINK}"
                echo -e "${C_GREEN}[+] ble-sniffer binary successfully symlinked to ${TARGET_SYMLINK}.${C_RESET}"
            fi

            if nrfutil ble-sniffer --help >/dev/null 2>&1 || nrfutil-ble-sniffer --help >/dev/null 2>&1; then
                echo -e "${C_GREEN}[+] nrfutil ble-sniffer plugin verified and operational.${C_RESET}"
            else
                echo -e "${C_YELLOW}[!] Warning: nrfutil ble-sniffer verification returned non-zero status.${C_RESET}"
            fi
        else
            echo -e "${C_YELLOW}[!] Could not automatically locate or install nrfutil-ble-sniffer.${C_RESET}"
            echo -e "${C_YELLOW}[!] Please run 'nrfutil install ble-sniffer' and symlink ~/.nrfutil/bin/nrfutil-ble-sniffer to /usr/local/bin/.${C_RESET}"
        fi
    fi
    echo ""
else
    echo -e "${C_YELLOW}[!] Skipping nrfutil installation (--no-nrf).${C_RESET}\n"
fi

# ------------------------------------------------------------------------------
# 3. USB Permissions & udev Rules for Nordic Sniffers
# ------------------------------------------------------------------------------
echo -e "${C_BOLD}${C_GREEN}[3/6] Setting Up USB Permissions & udev Rules...${C_RESET}"
UDEV_RULE_FILE="/etc/udev/rules.d/99-nrf-sniffer.rules"

if [ -d "/etc/udev/rules.d" ]; then
    EXPECTED_RULES='# Nordic Semiconductor nRF52840 Dongle / DevKit USB rules for non-root BLE sniffing
SUBSYSTEM=="tty", ATTRS{idVendor}=="1915", MODE="0666", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1915", MODE="0666", GROUP="plugdev"'

    CURRENT_RULES=$(cat "${UDEV_RULE_FILE}" 2>/dev/null || true)
    if [ "${CURRENT_RULES}" = "${EXPECTED_RULES}" ]; then
        echo -e "${C_GREEN}[+] udev rule ${UDEV_RULE_FILE} is already in place.${C_RESET}"
    else
        echo -e "${C_CYAN}[*] Writing ${UDEV_RULE_FILE}...${C_RESET}"
        run_sudo bash -c "cat << 'EOF' > ${UDEV_RULE_FILE}
# Nordic Semiconductor nRF52840 Dongle / DevKit USB rules for non-root BLE sniffing
SUBSYSTEM==\"tty\", ATTRS{idVendor}==\"1915\", MODE=\"0666\", GROUP=\"dialout\"
SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"1915\", MODE=\"0666\", GROUP=\"plugdev\"
EOF"
        
        if command -v udevadm >/dev/null 2>&1; then
            run_sudo udevadm control --reload-rules || true
            run_sudo udevadm trigger || true
        fi
        echo -e "${C_GREEN}[+] udev rules installed and reloaded.${C_RESET}"
    fi
fi

# Add user to dialout and plugdev groups
if getent group dialout >/dev/null 2>&1; then
    if id -nG "${REAL_USER}" 2>/dev/null | grep -qw "dialout"; then
        echo -e "${C_GREEN}[+] User '${REAL_USER}' is already a member of 'dialout' group.${C_RESET}"
    else
        run_sudo usermod -a -G dialout "${REAL_USER}" || true
        echo -e "${C_GREEN}[+] Added user '${REAL_USER}' to group 'dialout'.${C_RESET}"
    fi
fi
if getent group plugdev >/dev/null 2>&1; then
    if id -nG "${REAL_USER}" 2>/dev/null | grep -qw "plugdev"; then
        echo -e "${C_GREEN}[+] User '${REAL_USER}' is already a member of 'plugdev' group.${C_RESET}"
    else
        run_sudo usermod -a -G plugdev "${REAL_USER}" || true
        echo -e "${C_GREEN}[+] Added user '${REAL_USER}' to group 'plugdev'.${C_RESET}"
    fi
fi
echo ""

# ------------------------------------------------------------------------------
# 4. Python Virtual Environment & Monorepo Package Installation
# ------------------------------------------------------------------------------
echo -e "${C_BOLD}${C_GREEN}[4/6] Setting Up Python Virtual Environment & Installing Package...${C_RESET}"
VENV_DIR="${REPO_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

# Verify virtualenv exists AND pip is functional inside it
if [ -f "${VENV_PYTHON}" ] && [ -x "${VENV_PYTHON}" ] && "${VENV_PYTHON}" -m pip --version >/dev/null 2>&1; then
    echo -e "${C_GREEN}[+] Python virtualenv verified at ${VENV_DIR}.${C_RESET}"
else
    echo -e "${C_CYAN}[*] Creating clean Python virtualenv at ${VENV_DIR}...${C_RESET}"
    rm -rf "${VENV_DIR}"
    python3 -m venv "${VENV_DIR}" || {
        echo -e "${C_YELLOW}[!] Standard venv creation failed. Attempting with --without-pip and bootstrapping ensurepip...${C_RESET}"
        python3 -m venv --without-pip "${VENV_DIR}"
        "${VENV_PYTHON}" -m ensurepip --upgrade 2>/dev/null || {
            curl -fsSL https://bootstrap.pypa.io/get-pip.py | "${VENV_PYTHON}"
        }
    }
fi

echo -e "${C_CYAN}[*] Upgrading pip, setuptools, and wheel...${C_RESET}"
"${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true

echo -e "${C_CYAN}[*] Installing/updating drone-remote-id package with CLI tools (pip install -e '.[all]')...${C_RESET}"
(cd "${REPO_DIR}" && "${VENV_PYTHON}" -m pip install -e ".[all]" >/dev/null 2>&1 || "${VENV_PYTHON}" -m pip install -e ".[all]")
echo -e "${C_GREEN}[+] Python package and CLI executables verified in ${VENV_DIR}/bin/.${C_RESET}\n"

# ------------------------------------------------------------------------------
# 5. Linux Raw Network Capabilities (setcap for rootless sniffing)
# ------------------------------------------------------------------------------
if [ "$SKIP_CAPS" = false ]; then
    echo -e "${C_BOLD}${C_GREEN}[5/6] Granting Linux Raw Socket Capabilities (setcap)...${C_RESET}"
    if command -v setcap >/dev/null 2>&1 && [ -f "${VENV_PYTHON}" ]; then
        # Resolve any symlink to the real python binary for setcap
        REAL_PYTHON_BIN=$(readlink -f "${VENV_PYTHON}")
        CURRENT_CAPS=$(getcap "${REAL_PYTHON_BIN}" 2>/dev/null || true)
        
        if echo "${CURRENT_CAPS}" | grep -q "cap_net_raw.*cap_net_admin"; then
            echo -e "${C_GREEN}[+] Capabilities (cap_net_raw,cap_net_admin+eip) are already configured on ${REAL_PYTHON_BIN}.${C_RESET}\n"
        else
            echo -e "${C_CYAN}[*] Setting cap_net_raw,cap_net_admin+eip on ${REAL_PYTHON_BIN}...${C_RESET}"
            run_sudo setcap cap_net_raw,cap_net_admin+eip "${REAL_PYTHON_BIN}" 2>/dev/null || {
                echo -e "${C_YELLOW}[!] Warning: setcap could not be applied. The scanner can still run via sudo.${C_RESET}"
            }
            if getcap "${REAL_PYTHON_BIN}" 2>/dev/null | grep -q "cap_net_raw"; then
                echo -e "${C_GREEN}[+] Capabilities configured successfully.${C_RESET}\n"
            fi
        fi
    else
        echo -e "${C_YELLOW}[!] setcap not found or python binary unavailable. Skipping.${C_RESET}\n"
    fi
else
    echo -e "${C_YELLOW}[!] Skipping Linux capabilities setup (--no-caps).${C_RESET}\n"
fi

# ------------------------------------------------------------------------------
# 6. Optional systemd Background Services Installation
# ------------------------------------------------------------------------------
echo -e "${C_BOLD}${C_GREEN}[6/6] systemd Background Service Units Setup...${C_RESET}"

if [ "$INSTALL_SERVICES" = true ]; then
    # Ensure scanner_config.json exists
    if [ ! -f "${SCANNER_CONFIG}" ]; then
        if [ -f "${REPO_DIR}/scanner/scanner_config.example.json" ]; then
            echo -e "${C_CYAN}[*] Initializing ${SCANNER_CONFIG} from example template...${C_RESET}"
            cp "${REPO_DIR}/scanner/scanner_config.example.json" "${SCANNER_CONFIG}"
        else
            echo -e "${C_CYAN}[*] Initializing default ${SCANNER_CONFIG}...${C_RESET}"
            "${VENV_PYTHON}" -c "from scanner.scanner_config import load_scanner_config; load_scanner_config('${SCANNER_CONFIG}')" 2>/dev/null || true
        fi
    fi

    # Update hub_url or node_id in scanner_config.json if specified on CLI
    if [ -n "${HUB_URL}" ] || [ -n "${NODE_ID}" ]; then
        "${VENV_PYTHON}" -c "
import json, os
path = '${SCANNER_CONFIG}'
try:
    with open(path, 'r') as f:
        data = json.load(f)
except Exception:
    data = {}
if '${HUB_URL}':
    data['hub_ws_url'] = '${HUB_URL}'
if '${NODE_ID}':
    data['node_id'] = '${NODE_ID}'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || true
    fi

    # Auto-detect Wi-Fi monitor interface if not supplied
    if [ -z "${WIFI_IFACE}" ]; then
        DETECTED_IFACE=$(iw dev 2>/dev/null | awk '$1=="Interface"{print $2}' | head -n 1 || true)
        WIFI_IFACE="${DETECTED_IFACE:-wlan0}"
    fi

    # Auto-detect nRF port if not supplied
    if [ -z "${NRF_PORT}" ]; then
        DETECTED_NRF=$(ls /dev/ttyACM* 2>/dev/null | head -n 1 || true)
        NRF_PORT="${DETECTED_NRF:-/dev/ttyACM0}"
    fi

    echo -e "${C_CYAN}[*] Generating customized drone-scanner.service for this node...${C_RESET}"
    echo -e "    - WorkingDirectory: ${REPO_DIR}"
    echo -e "    - Wi-Fi Interface : ${WIFI_IFACE}"
    echo -e "    - nRF Port        : ${NRF_PORT}"
    echo -e "    - Scanner Config  : ${SCANNER_CONFIG}"

    # drone-scanner.service
    TMP_SCANNER_SRV="/tmp/drone-scanner-$$.service"
    cat << EOF > "${TMP_SCANNER_SRV}"
[Unit]
Description=Autonomous ASTM F3411 Drone Remote ID Combined Scanner (Wi-Fi + BLE)
Documentation=https://github.com/cyber-defence-campus/droneRemoteIDSpoofer
After=network.target time-sync.target
Wants=time-sync.target

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_DIR}/bin/drone-scanner \\
    --wifi-iface ${WIFI_IFACE} \\
    --nrf-port ${NRF_PORT} \\
    --ble-mode extended \\
    --coded \\
    --scanner-config ${SCANNER_CONFIG} \\
    --quiet

Restart=always
RestartSec=5s
KillMode=mixed
TimeoutStopSec=10s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drone-scanner

[Install]
WantedBy=multi-user.target
EOF

    run_sudo mv "${TMP_SCANNER_SRV}" /etc/systemd/system/drone-scanner.service
    run_sudo chmod 644 /etc/systemd/system/drone-scanner.service
    run_sudo systemctl daemon-reload
    run_sudo systemctl enable drone-scanner.service || true

    echo -e "${C_GREEN}[+] Scanner service installed and enabled:${C_RESET}"
    echo "      sudo systemctl start drone-scanner.service"
    echo "      sudo systemctl status drone-scanner.service"
    echo "      sudo journalctl -u drone-scanner.service -f"
fi

if [ "$INSTALL_DASHBOARD_SERVICE" = true ]; then
    # Ensure dashboard_config.json exists
    if [ ! -f "${DASHBOARD_CONFIG}" ]; then
        if [ -f "${REPO_DIR}/scanner/dashboard/dashboard_config.example.json" ]; then
            echo -e "${C_CYAN}[*] Initializing ${DASHBOARD_CONFIG} from example template...${C_RESET}"
            mkdir -p "$(dirname "${DASHBOARD_CONFIG}")"
            cp "${REPO_DIR}/scanner/dashboard/dashboard_config.example.json" "${DASHBOARD_CONFIG}"
        else
            echo -e "${C_CYAN}[*] Initializing default ${DASHBOARD_CONFIG}...${C_RESET}"
            "${VENV_PYTHON}" -c "from scanner.dashboard.dashboard_config import load_dashboard_config; load_dashboard_config('${DASHBOARD_CONFIG}')" 2>/dev/null || true
        fi
    fi

    echo -e "${C_CYAN}[*] Generating customized drone-dashboard.service for this server/node...${C_RESET}"
    echo -e "    - WorkingDirectory: ${REPO_DIR}"
    echo -e "    - Dashboard Config: ${DASHBOARD_CONFIG}"

    TMP_DASH_SRV="/tmp/drone-dashboard-$$.service"
    cat << EOF > "${TMP_DASH_SRV}"
[Unit]
Description=Tactical ASTM F3411 Drone Remote ID Web Dashboard & Airspace Radar
Documentation=https://github.com/cyber-defence-campus/droneRemoteIDSpoofer
After=network.target time-sync.target
Wants=time-sync.target

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_DIR}/bin/drone-dashboard \\
    --host 0.0.0.0 \\
    --port 8080 \\
    --dashboard-config ${DASHBOARD_CONFIG}

Restart=always
RestartSec=5s
KillMode=mixed
TimeoutStopSec=10s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drone-dashboard

[Install]
WantedBy=multi-user.target
EOF

    run_sudo mv "${TMP_DASH_SRV}" /etc/systemd/system/drone-dashboard.service
    run_sudo chmod 644 /etc/systemd/system/drone-dashboard.service
    run_sudo systemctl daemon-reload
    run_sudo systemctl enable drone-dashboard.service || true

    echo -e "${C_GREEN}[+] Dashboard service installed and enabled:${C_RESET}"
    echo "      sudo systemctl start drone-dashboard.service"
    echo "      sudo systemctl status drone-dashboard.service"
    echo "      sudo journalctl -u drone-dashboard.service -f"
fi

if [ "$INSTALL_HUB_SERVICE" = true ]; then
    echo -e "${C_CYAN}[*] Generating customized drone-central-hub.service for this server...${C_RESET}"
    echo -e "    - WorkingDirectory: ${REPO_DIR}"

    TMP_HUB_SRV="/tmp/drone-central-hub-$$.service"
    cat << EOF > "${TMP_HUB_SRV}"
[Unit]
Description=Central Drone Remote ID Ingestion Hub & Sensor Fusion Server
Documentation=https://github.com/cyber-defence-campus/droneRemoteIDSpoofer
After=network.target time-sync.target
Wants=time-sync.target

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_DIR}/bin/drone-central-hub \\
    --host 0.0.0.0 \\
    --port 8000 \\
    --db-file ${REPO_DIR}/rid_detections_central.db \\
    --log-dir ${REPO_DIR}/central_logs \\
    --timeout-s 300.0 \\
    --quiet

Restart=always
RestartSec=5s
KillMode=mixed
TimeoutStopSec=10s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=drone-central-hub

[Install]
WantedBy=multi-user.target
EOF

    run_sudo mv "${TMP_HUB_SRV}" /etc/systemd/system/drone-central-hub.service
    run_sudo chmod 644 /etc/systemd/system/drone-central-hub.service
    run_sudo systemctl daemon-reload
    run_sudo systemctl enable drone-central-hub.service || true

    echo -e "${C_GREEN}[+] Central Hub service installed and enabled:${C_RESET}"
    echo "      sudo systemctl start drone-central-hub.service"
    echo "      sudo systemctl status drone-central-hub.service"
    echo "      sudo journalctl -u drone-central-hub.service -f"
fi
echo ""

# ------------------------------------------------------------------------------
# Summary & Quickstart
# ------------------------------------------------------------------------------
echo -e "${C_GREEN}${C_BOLD}"
echo "=============================================================================="
echo "  INSTALLATION COMPLETE!"
echo "=============================================================================="
echo -e "${C_RESET}"
echo -e "${C_BOLD}Installed CLI Commands (in .venv/bin):${C_RESET}"
echo -e "  - ${C_CYAN}drone-scanner${C_RESET}    : Live Wi-Fi + BLE 4/5 Remote ID scanner and logger"
echo -e "  - ${C_CYAN}drone-dashboard${C_RESET}  : Tactical Radar & Deep Packet Inspector Web UI"
echo -e "  - ${C_CYAN}drone-query${C_RESET}      : SQLite database search, statistics & GeoJSON export"
echo -e "  - ${C_CYAN}drone-spoofer${C_RESET}    : Transmitter & ASTM F3411 packet spoofer"
echo -e "  - ${C_CYAN}drone-replay${C_RESET}     : Replay captured RF traffic over the air"
echo ""
echo -e "${C_BOLD}Quickstart Examples:${C_RESET}"
echo -e "  ${C_YELLOW}# 1. Run the combined scanner (Wi-Fi + BLE):${C_RESET}"
echo "     sudo ${VENV_DIR}/bin/drone-scanner --wifi-iface wlan1 --nrf-port /dev/ttyACM0"
echo ""
echo -e "  ${C_YELLOW}# 2. Launch the Tactical Web Dashboard:${C_RESET}"
echo "     ${VENV_DIR}/bin/drone-dashboard --port 8080"
echo "     Open: http://localhost:8080"
echo ""
echo -e "  ${C_YELLOW}# 3. Search and export recorded flights:${C_RESET}"
echo "     ${VENV_DIR}/bin/drone-query list"
echo "     ${VENV_DIR}/bin/drone-query export-geojson <ENCOUNTER_ID> -o flight.geojson"
echo ""
if [ "$REAL_USER" != "root" ]; then
    echo -e "${C_YELLOW}[!] Reminder:${C_RESET} If you were added to the 'dialout' group, log out and back in (or run 'newgrp dialout') for USB serial port permissions to take effect."
fi
echo ""
