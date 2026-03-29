#!/bin/bash
# ==============================================================================
# Node Exporter - systemd Service Installer
# ==============================================================================
# Installs node_exporter as a systemd service.
# Place the node_exporter binary in EXPORTER_DIR before running this script.
# Run with: sudo bash install-node-exporter.sh
# ==============================================================================

# --- CONFIGURATION ------------------------------------------------------------
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
EXPORTER_DIR="$REAL_HOME/Apps/node_exporter"   # Change this if needed
# ------------------------------------------------------------------------------

BINARY="$EXPORTER_DIR/node_exporter"
SERVICE_NAME="node-exporter"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="node_exporter"

set -euo pipefail

# ---- Helpers -----------------------------------------------------------------
info()    { echo -e "\e[32m[INFO]\e[0m  $*"; }
warn()    { echo -e "\e[33m[WARN]\e[0m  $*"; }
error()   { echo -e "\e[31m[ERROR]\e[0m $*" >&2; exit 1; }

# ---- Pre-flight checks -------------------------------------------------------
[[ $EUID -ne 0 ]] && error "Please run as root (sudo bash $0)"

info "Installer starting..."
info "Exporter directory : $EXPORTER_DIR"
info "Binary path        : $BINARY"

[[ ! -d "$EXPORTER_DIR" ]] && error "Directory not found: $EXPORTER_DIR"
[[ ! -f "$BINARY" ]]       && error "Binary not found: $BINARY  — please place node_exporter there first."
[[ ! -x "$BINARY" ]]       && { warn "Binary is not executable. Fixing..."; chmod +x "$BINARY"; }

# ---- Create dedicated system user --------------------------------------------
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating system user: $SERVICE_USER"
    useradd --system --no-create-home --shell /bin/false "$SERVICE_USER"
else
    info "System user '$SERVICE_USER' already exists — skipping."
fi

# ---- Set ownership -----------------------------------------------------------
info "Setting ownership of $EXPORTER_DIR to $SERVICE_USER"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$EXPORTER_DIR"

# ---- Write systemd unit file -------------------------------------------------
info "Writing service file: $SERVICE_FILE"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Prometheus Node Exporter
Documentation=https://github.com/prometheus/node_exporter
After=network.target

[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
Type=simple
ExecStart=${BINARY}
Restart=on-failure
RestartSec=5s

# Hardening
NoNewPrivileges=yes
ProtectSystem=strict
PrivateTmp=yes
ReadOnlyPaths=${EXPORTER_DIR}

[Install]
WantedBy=multi-user.target
EOF

# ---- Enable & start service --------------------------------------------------
info "Reloading systemd daemon..."
systemctl daemon-reload

info "Enabling ${SERVICE_NAME} to start on boot..."
systemctl enable "$SERVICE_NAME"

info "Starting ${SERVICE_NAME}..."
systemctl start "$SERVICE_NAME"

# ---- Status ------------------------------------------------------------------
sleep 1
if systemctl is-active --quiet "$SERVICE_NAME"; then
    info "✅ ${SERVICE_NAME} is running."
    echo ""
    systemctl status "$SERVICE_NAME" --no-pager -l
    echo ""
    info "Metrics available at: http://localhost:9100/metrics"
else
    error "${SERVICE_NAME} failed to start. Run: journalctl -u ${SERVICE_NAME} -n 50"
fi
