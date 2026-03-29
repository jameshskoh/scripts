#!/bin/bash

# ── Configuration ──────────────────────────────────────────────────────────────
CHECK_URLS=("https://google.com" "https://cloudflare.com")
MAX_FAILURES=5
CHECK_INTERVAL=300
CURL_TIMEOUT=10
LOG_FILE="/var/log/network_watchdog.log"
# ───────────────────────────────────────────────────────────────────────────────

consecutive_failures=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

flush_dns() {
    log "Attempting DNS cache flush..."
    if systemctl is-active --quiet systemd-resolved; then
        sudo resolvectl flush-caches && log "DNS flushed via resolvectl." && return 0
    fi
    if systemctl is-active --quiet dnsmasq; then
        sudo pkill -HUP dnsmasq && log "DNS flushed via dnsmasq (SIGHUP)." && return 0
    fi
    if systemctl is-active --quiet nscd; then
        sudo systemctl restart nscd && log "DNS flushed via nscd restart." && return 0
    fi
    log "No known DNS cache service found — skipping flush."
    return 1
}

check_network() {
    for url in "${CHECK_URLS[@]}"; do
        if curl --silent --max-time "$CURL_TIMEOUT" --output /dev/null "$url"; then
            return 0
        fi
    done
    return 1
}

log "Network watchdog started. Max failures: $MAX_FAILURES, Interval: ${CHECK_INTERVAL}s"

while true; do
    if check_network; then
        if [ "$consecutive_failures" -gt 0 ]; then
            log "Network restored after $consecutive_failures failure(s)."
        fi
        consecutive_failures=0
    else
        consecutive_failures=$((consecutive_failures + 1))
        log "Network check FAILED ($consecutive_failures/$MAX_FAILURES)."

        flush_dns

        if [ "$consecutive_failures" -ge "$MAX_FAILURES" ]; then
            log "Threshold reached — rebooting now."
            sudo reboot
        fi
    fi

    sleep "$CHECK_INTERVAL"
done