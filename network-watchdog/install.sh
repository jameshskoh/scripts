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
    echo "Please run as root: sudo ./install.sh"
    exit 1
fi

echo "[1/5] Copying watchdog script to $INSTALL_PATH..."
cp "$SCRIPT_NAME" "$INSTALL_PATH"
chmod +x "$INSTALL_PATH"

echo "[2/5] Creating log file at $LOG_FILE..."
touch "$LOG_FILE"
chmod 644 "$LOG_FILE"

echo "[3/5] Writing systemd service file to $SERVICE_PATH..."
cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Network Watchdog - Reboot on connectivity loss
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$INSTALL_PATH
Restart=on-failure
RestartSec=10
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
EOF

echo "[4/5] Reloading systemd and enabling service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "[5/5] Starting service..."
systemctl start "$SERVICE_NAME"

echo ""
echo "✅ Done! Network watchdog is installed and running."
echo ""
echo "Useful commands:"
echo "  Status  : sudo systemctl status $SERVICE_NAME"
echo "  Logs    : tail -f $LOG_FILE"
echo "  Stop    : sudo systemctl stop $SERVICE_NAME"
echo "  Disable : sudo systemctl disable $SERVICE_NAME"