#!/bin/bash
# ==============================================================================
# Node Exporter - systemd Service Uninstaller
# ==============================================================================
# Stops and removes the node_exporter systemd service.
# The binary/directory itself is NOT deleted (only the service is removed).
# Run with: sudo bash uninstall-node-exporter.sh
# ==============================================================================

# --- CONFIGURATION ------------------------------------------------------------
EXPORTER_DIR="/opt/node_exporter"   # Change this if needed
# ------------------------------------------------------------------------------

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

info "Uninstaller starting..."
info "Service name       : $SERVICE_NAME"
info "Exporter directory : $EXPORTER_DIR"

# ---- Stop service ------------------------------------------------------------
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    info "Stopping ${SERVICE_NAME}..."
    systemctl stop "$SERVICE_NAME"
else
    warn "Service '${SERVICE_NAME}' is not running — skipping stop."
fi

# ---- Disable service ---------------------------------------------------------
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    info "Disabling ${SERVICE_NAME} from startup..."
    systemctl disable "$SERVICE_NAME"
else
    warn "Service '${SERVICE_NAME}' is not enabled — skipping disable."
fi

# ---- Remove service file -----------------------------------------------------
if [[ -f "$SERVICE_FILE" ]]; then
    info "Removing service file: $SERVICE_FILE"
    rm -f "$SERVICE_FILE"
else
    warn "Service file not found: $SERVICE_FILE — skipping."
fi

# ---- Reload systemd ----------------------------------------------------------
info "Reloading systemd daemon..."
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

# ---- Remove dedicated system user --------------------------------------------
if id "$SERVICE_USER" &>/dev/null; then
    info "Removing system user: $SERVICE_USER"
    userdel "$SERVICE_USER"
else
    warn "System user '$SERVICE_USER' not found — skipping."
fi

# ---- Done --------------------------------------------------------------------
echo ""
info "✅ node-exporter service has been fully removed."
info "   The binary and directory ($EXPORTER_DIR) were left untouched."
info "   Delete them manually if you no longer need them."
