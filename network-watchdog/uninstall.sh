#!/bin/bash

# ── Configuration ──────────────────────────────────────────────────────────────
SCRIPT_NAME="network_watchdog.sh"
SERVICE_NAME="network-watchdog"
INSTALL_PATH="/usr/local/bin/$SCRIPT_NAME"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME.service"
LOG_FILE="/var/log/network_watchdog.log"
# ───────────────────────────────────────────────────────────────────────────────

# Must be run as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo ./uninstall.sh"
    exit 1
fi

echo "[1/4] Stopping and disabling service..."
systemctl stop "$SERVICE_NAME"
systemctl disable "$SERVICE_NAME"

echo "[2/4] Removing systemd service file..."
rm -f "$SERVICE_PATH"
systemctl daemon-reload

echo "[3/4] Removing watchdog script from $INSTALL_PATH..."
rm -f "$INSTALL_PATH"

echo "[4/4] Removing log file..."
rm -f "$LOG_FILE"

echo ""
echo "Done! Network watchdog has been removed."
